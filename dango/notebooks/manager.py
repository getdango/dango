"""dango/notebooks/manager.py

Marimo notebook server process lifecycle (start, stop, status) and idle
auto-shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from dango.utils.dango_db import connect
from dango.utils.process import is_process_running, kill_process

logger = logging.getLogger(__name__)

# --- Idle auto-shutdown state ---
_idle_checker_task: asyncio.Task[None] | None = None
_IDLE_CHECK_INTERVAL = 300  # 5 minutes
_IDLE_TIMEOUT_LOCAL = 7200  # 2 hours
_IDLE_TIMEOUT_CLOUD = 3600  # 1 hour
_IDLE_TIMEOUT = _IDLE_TIMEOUT_LOCAL  # backward compat for smoke test


def _get_idle_timeout(project_root: Path) -> int:
    """Return the idle timeout in seconds based on deployment mode."""
    from dango.config.helpers import is_cloud_mode

    if is_cloud_mode(project_root):
        return _IDLE_TIMEOUT_CLOUD
    return _IDLE_TIMEOUT_LOCAL


def get_marimo_pid_file_path(project_root: Path) -> Path:
    """Get path to PID file for Marimo server.

    Args:
        project_root: Project root directory.

    Returns:
        Path to the Marimo PID file.
    """
    return project_root / ".dango" / "marimo.pid"


def start_marimo(
    project_root: Path,
    port: int | None = None,
    snapshot_path: Path | None = None,
) -> int | None:
    """Start Marimo notebook server in background.

    Args:
        project_root: Project root directory.
        port: Port to listen on.  Defaults to ``PlatformSettings.marimo_port``.
        snapshot_path: If provided, set ``DANGO_NOTEBOOK_DB_PATH`` env var so
            templates connect to a read-only snapshot instead of the live warehouse.

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

    # Marimo's --timeout and Dango's _idle_check_loop use the same value
    # intentionally — Marimo is a fallback if the Dango checker fails.
    timeout_minutes = _get_idle_timeout(project_root) // 60

    env = os.environ.copy()
    if snapshot_path is not None:
        env["DANGO_NOTEBOOK_DB_PATH"] = str(snapshot_path)

    log_handle = None
    try:
        log_handle = open(log_file, "w")  # noqa: SIM115

        cmd = [
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
            str(timeout_minutes),
            "--skip-update-check",
        ]

        # Add base-url for cloud proxy so SPA assets use correct prefix
        from dango.config.helpers import is_cloud_mode

        if is_cloud_mode(project_root):
            cmd.extend(["--base-url", "/notebooks/marimo"])

        cmd.append(str(notebooks_dir))

        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
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


async def _broadcast_idle_warning(remaining_seconds: int) -> None:
    """Broadcast a WebSocket warning that Marimo will shut down soon."""
    try:
        from dango.web.routes.websocket import ws_manager

        minutes_left = max(1, remaining_seconds // 60)
        await ws_manager.broadcast(
            {
                "event": "notebook_idle_warning",
                "message": (
                    f"Notebook server will shut down in {minutes_left} minutes due to inactivity."
                ),
                "remaining_seconds": remaining_seconds,
                "timestamp": datetime.now().isoformat(),
            }
        )
    except Exception:
        pass  # web server may not be running for CLI-launched notebooks


async def _idle_check_loop(project_root: Path) -> None:
    """Background loop: shut down Marimo when idle for the configured timeout."""
    idle_since: float = time.monotonic()
    timeout = _get_idle_timeout(project_root)
    warning_sent = False

    while True:
        await asyncio.sleep(_IDLE_CHECK_INTERVAL)

        has_locks = await asyncio.to_thread(_has_active_locks, project_root)
        if has_locks:
            idle_since = 0.0  # reset — will be set on next no-lock check
            warning_sent = False
            continue

        if idle_since == 0.0:
            idle_since = time.monotonic()

        elapsed = time.monotonic() - idle_since

        # Warn ~5 min before shutdown
        if not warning_sent and elapsed >= timeout - 300:
            remaining = max(0, int(timeout - elapsed))
            await _broadcast_idle_warning(remaining)
            warning_sent = True

        if elapsed >= timeout:
            logger.info("Marimo idle for %ds — shutting down", timeout)
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
