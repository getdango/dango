"""dango/platform/local/watcher_lifecycle.py

Watcher subprocess lifecycle management (start, stop, status).

Moved from cli/utils.py to fix the web→cli architecture violation:
web/routes/health.py needs get_watcher_status, which belongs at Level 2 (platform/)
not Level 3 (cli/).
"""

import logging
import subprocess
import time
from pathlib import Path

from dango.utils.process import (
    is_process_running,
    kill_process,
    read_pid_record,
    write_pid_record,
)

logger = logging.getLogger(__name__)


def get_watcher_pid_file_path(project_root: Path) -> Path:
    """Get path to PID file for file watcher."""
    return project_root / ".dango" / "watcher.pid"


def kill_orphan_watchers(project_root: Path) -> int:
    """Scan for and kill orphaned watcher_runner.py processes for this project.

    Catches orphans that have no PID file (e.g., parent crashed before writing it).
    Returns count of killed orphans.
    """
    import os

    import psutil

    resolved_root = str(project_root.resolve())
    killed = 0
    current_pid = os.getpid()

    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.pid == current_pid:
                continue
            cmdline = proc.info.get("cmdline") or []
            if not cmdline:
                continue
            has_runner = any("watcher_runner.py" in arg for arg in cmdline)
            has_root = any(resolved_root in arg for arg in cmdline)
            if has_runner and has_root:
                logger.warning("Killing orphaned watcher process (PID %d)", proc.pid)
                kill_process(proc.pid)
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    return killed


def start_file_watcher(project_root: Path) -> int | None:
    """
    Start file watcher in background.

    Args:
        project_root: Project root directory

    Returns:
        PID of started process, or None if failed

    Raises:
        RuntimeError: If watcher is already running or fails to start
    """
    import sys

    # Check if we already have a PID file first
    pid_file = get_watcher_pid_file_path(project_root)
    if pid_file.exists():
        existing_record = read_pid_record(pid_file)
        if existing_record is None:
            # Invalid/unparseable PID file, remove it
            try:
                pid_file.unlink()
            except OSError:  # noqa: BLE001
                pass
        elif is_process_running(
            existing_record.pid, expected_start_time=existing_record.start_time
        ):
            raise RuntimeError(
                f"File watcher is already running (PID {existing_record.pid}).\n"
                f"Stop it with 'dango stop'"
            )
        else:
            # Stale PID file (or a PID the OS has reused for a different process —
            # see 1.0.8-OPS-1), remove it
            try:
                pid_file.unlink()
            except OSError:  # noqa: BLE001
                pass

    # Log file for watcher output
    log_file = project_root / ".dango" / "watcher.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Prevent fd 0 clobbering: see ensure_std_fds() docstring for details.
    from dango.utils.process import ensure_std_fds

    ensure_std_fds()

    try:
        # Open log file
        log_handle = open(log_file, "w")  # noqa: SIM115

        # Get path to watcher_runner.py (in same directory as this file)
        watcher_runner = Path(__file__).parent / "watcher_runner.py"

        # Start watcher runner
        proc = subprocess.Popen(
            [sys.executable, str(watcher_runner), str(project_root)],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # Detach from parent session
        )

        # Give watcher a moment to start
        time.sleep(1)

        # Check if process is still running
        if proc.poll() is not None:
            # Process exited immediately, something went wrong
            log_handle.close()
            raise RuntimeError(f"File watcher failed to start. Check logs at {log_file}")

        # Write PID file (with start time, for identity verification — see 1.0.8-OPS-1)
        write_pid_record(pid_file, proc.pid)

        # Don't close log_handle - let subprocess write to it

        return proc.pid

    except Exception as e:
        if "log_handle" in locals():
            log_handle.close()
        raise RuntimeError(f"Failed to start file watcher: {e}") from e


def stop_file_watcher(project_root: Path) -> bool:
    """
    Stop file watcher.

    Args:
        project_root: Project root directory

    Returns:
        True if watcher was stopped, False if it wasn't running
    """
    pid_file = get_watcher_pid_file_path(project_root)

    if not pid_file.exists():
        logger.debug("No file watcher PID file found")
        return False

    record = read_pid_record(pid_file)
    if record is None:
        logger.warning("Invalid file watcher PID file")
        pid_file.unlink()
        return False
    pid = record.pid

    if not is_process_running(pid, expected_start_time=record.start_time):
        logger.debug("File watcher PID %d is not running (stale PID file)", pid)
        pid_file.unlink()
        return False

    logger.debug("Stopping file watcher (PID %d)", pid)

    # Try to kill the process — identity-verified, so a stale PID file pointing at
    # a reused PID is never signaled (see 1.0.8-OPS-1).
    success = kill_process(pid, timeout=10, expected_start_time=record.start_time)

    # Clean up PID file
    try:
        pid_file.unlink()
    except OSError:  # noqa: BLE001
        pass

    if success:
        logger.debug("File watcher stopped")
        return True
    else:
        logger.warning("Failed to stop file watcher process %d", pid)
        return False


def get_watcher_status(project_root: Path) -> dict:
    """
    Get file watcher status.

    Args:
        project_root: Project root directory

    Returns:
        Dict with status info:
            - running: bool
            - pid: Optional[int]
            - log_file: Path
    """
    pid_file = get_watcher_pid_file_path(project_root)
    log_file = project_root / ".dango" / "watcher.log"

    status: dict[str, bool | int | Path | None] = {
        "running": False,
        "pid": None,
        "log_file": log_file,
    }

    if pid_file.exists():
        record = read_pid_record(pid_file)
        if record and is_process_running(record.pid, expected_start_time=record.start_time):
            status["running"] = True
            status["pid"] = record.pid

    return status
