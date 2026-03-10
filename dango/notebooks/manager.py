"""dango/notebooks/manager.py

Marimo notebook server process lifecycle (start, stop, status).
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from dango.utils.process import is_process_running, kill_process

logger = logging.getLogger(__name__)


def get_marimo_pid_file_path(project_root: Path) -> Path:
    """Get path to PID file for Marimo server.

    Args:
        project_root: Project root directory.

    Returns:
        Path to the Marimo PID file.
    """
    return project_root / ".dango" / "marimo.pid"


def start_marimo(project_root: Path, port: int | None = None) -> int | None:
    """Start Marimo notebook server in background.

    Args:
        project_root: Project root directory.
        port: Port to listen on.  Defaults to ``PlatformSettings.marimo_port``.

    Returns:
        PID of started process, or ``None`` if failed.

    Raises:
        RuntimeError: If Marimo is already running or fails to start.
    """
    import sys

    pid_file = get_marimo_pid_file_path(project_root)
    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text().strip())
            if is_process_running(existing_pid):
                raise RuntimeError(
                    f"Marimo is already running (PID {existing_pid}).\nStop it with 'dango stop'"
                )
            else:
                pid_file.unlink()
        except (ValueError, OSError):
            pid_file.unlink()

    if port is None:
        from dango.config.loader import ConfigLoader

        loader = ConfigLoader(project_root)
        config = loader.load_config()
        port = config.platform.marimo_port

    notebooks_dir = project_root / "notebooks"
    notebooks_dir.mkdir(parents=True, exist_ok=True)

    log_file = project_root / ".dango" / "marimo.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    log_handle = None
    try:
        log_handle = open(log_file, "w")  # noqa: SIM115

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "marimo",
                "edit",
                "--headless",
                "--no-token",
                "--port",
                str(port),
                "--host",
                "127.0.0.1",
                "--timeout",
                "30",
                "--skip-update-check",
                str(notebooks_dir),
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        time.sleep(1)

        if proc.poll() is not None:
            raise RuntimeError(f"Marimo failed to start. Check logs at {log_file}")

        pid_file.write_text(str(proc.pid))

        return proc.pid

    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to start Marimo: {e}") from e
    finally:
        if log_handle is not None:
            log_handle.close()


def stop_marimo(project_root: Path) -> bool:
    """Stop Marimo notebook server.

    Args:
        project_root: Project root directory.

    Returns:
        ``True`` if Marimo was stopped, ``False`` if it wasn't running.
    """
    pid_file = get_marimo_pid_file_path(project_root)

    if not pid_file.exists():
        logger.debug("No Marimo PID file found")
        return False

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        logger.warning("Invalid Marimo PID file")
        pid_file.unlink()
        return False

    if not is_process_running(pid):
        logger.debug("Marimo PID %d is not running (stale PID file)", pid)
        pid_file.unlink()
        return False

    logger.debug("Stopping Marimo (PID %d)", pid)

    success = kill_process(pid, timeout=10)

    try:
        pid_file.unlink()
    except OSError:
        pass

    if success:
        logger.debug("Marimo stopped")
        return True
    else:
        logger.warning("Failed to stop Marimo process %d", pid)
        return False


def get_marimo_status(project_root: Path) -> dict[str, bool | int | Path | None]:
    """Get Marimo notebook server status.

    Args:
        project_root: Project root directory.

    Returns:
        Dict with keys: ``running`` (bool), ``pid`` (int | None),
        ``log_file`` (Path), ``port`` (int | None).
    """
    pid_file = get_marimo_pid_file_path(project_root)
    log_file = project_root / ".dango" / "marimo.log"

    status: dict[str, bool | int | Path | None] = {
        "running": False,
        "pid": None,
        "log_file": log_file,
        "port": None,
    }

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if is_process_running(pid):
                status["running"] = True
                status["pid"] = pid

                from dango.config.loader import ConfigLoader

                try:
                    loader = ConfigLoader(project_root)
                    config = loader.load_config()
                    status["port"] = config.platform.marimo_port
                except Exception:
                    status["port"] = 7805
        except (ValueError, OSError):
            pass

    return status
