"""dango/platform/watcher_lifecycle.py

Watcher subprocess lifecycle management (start, stop, status).

Moved from cli/utils.py to fix the web→cli architecture violation:
web/routes/health.py needs get_watcher_status, which belongs at Level 2 (platform/)
not Level 3 (cli/).
"""

import logging
import subprocess
import time
from pathlib import Path

from dango.utils.process import is_process_running, kill_process

logger = logging.getLogger(__name__)


def get_watcher_pid_file_path(project_root: Path) -> Path:
    """Get path to PID file for file watcher."""
    return project_root / ".dango" / "watcher.pid"


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
        try:
            existing_pid = int(pid_file.read_text().strip())
            if is_process_running(existing_pid):
                raise RuntimeError(
                    f"File watcher is already running (PID {existing_pid}).\n"
                    f"Stop it with 'dango stop'"
                )
            else:
                # Stale PID file, remove it
                pid_file.unlink()
        except (ValueError, OSError):
            # Invalid PID file, remove it
            pid_file.unlink()

    # Log file for watcher output
    log_file = project_root / ".dango" / "watcher.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Open log file
        log_handle = open(log_file, "w")  # noqa: SIM115

        # Get path to watcher_runner.py
        import dango.platform

        platform_dir = Path(dango.platform.__file__).parent
        watcher_runner = platform_dir / "watcher_runner.py"

        # Start watcher runner
        proc = subprocess.Popen(
            [sys.executable, str(watcher_runner), str(project_root)],
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

        # Write PID file
        pid_file.write_text(str(proc.pid))

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

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        logger.warning("Invalid file watcher PID file")
        pid_file.unlink()
        return False

    if not is_process_running(pid):
        logger.debug("File watcher PID %d is not running (stale PID file)", pid)
        pid_file.unlink()
        return False

    logger.debug("Stopping file watcher (PID %d)", pid)

    # Try to kill the process
    success = kill_process(pid, timeout=10)

    # Clean up PID file
    try:
        pid_file.unlink()
    except OSError:
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
        try:
            pid = int(pid_file.read_text().strip())
            if is_process_running(pid):
                status["running"] = True
                status["pid"] = pid
        except (ValueError, OSError):
            pass

    return status
