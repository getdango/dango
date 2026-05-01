"""dango/notebooks/manager.py

Marimo notebook server process lifecycle (start, stop, status) and idle
auto-shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path

from dango.utils.dango_db import connect
from dango.utils.process import is_process_running, kill_process

logger = logging.getLogger(__name__)

# --- Idle auto-shutdown state ---
_idle_checker_task: asyncio.Task[None] | None = None
_IDLE_CHECK_INTERVAL = 300  # 5 minutes
_IDLE_TIMEOUT = 900  # 15 minutes


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


def _has_active_locks(project_root: Path) -> bool:
    """Check if any non-expired notebook locks exist."""
    with connect(project_root) as conn:
        conn.execute("DELETE FROM notebook_locks WHERE expires_at < datetime('now')")
        conn.commit()
        row = conn.execute("SELECT COUNT(*) AS cnt FROM notebook_locks").fetchone()
        return bool(row and row["cnt"] > 0)


def _release_all_locks(project_root: Path) -> None:
    """Delete all notebook locks (used during idle shutdown)."""
    with connect(project_root) as conn:
        conn.execute("DELETE FROM notebook_locks")
        conn.commit()
    logger.info("Released all notebook locks (idle shutdown)")


async def _idle_check_loop(project_root: Path) -> None:
    """Background loop: shut down Marimo when idle for ``_IDLE_TIMEOUT`` seconds."""
    idle_since: float = time.monotonic()

    while True:
        await asyncio.sleep(_IDLE_CHECK_INTERVAL)

        has_locks = await asyncio.to_thread(_has_active_locks, project_root)
        if has_locks:
            idle_since = 0.0  # reset — will be set on next no-lock check
            continue

        if idle_since == 0.0:
            idle_since = time.monotonic()

        if time.monotonic() - idle_since >= _IDLE_TIMEOUT:
            logger.info("Marimo idle for %ds — shutting down", _IDLE_TIMEOUT)
            # Clean up any locks created in the narrow window since last check
            if await asyncio.to_thread(_has_active_locks, project_root):
                await asyncio.to_thread(_release_all_locks, project_root)
            await asyncio.to_thread(stop_marimo, project_root)
            break


def start_idle_checker(project_root: Path) -> None:
    """Start the idle-shutdown background task (idempotent)."""
    global _idle_checker_task  # noqa: PLW0603
    if _idle_checker_task is not None and not _idle_checker_task.done():
        return
    _idle_checker_task = asyncio.get_running_loop().create_task(_idle_check_loop(project_root))


def stop_idle_checker() -> None:
    """Cancel the idle-shutdown background task if running."""
    global _idle_checker_task  # noqa: PLW0603
    if _idle_checker_task is not None and not _idle_checker_task.done():
        _idle_checker_task.cancel()
    _idle_checker_task = None
