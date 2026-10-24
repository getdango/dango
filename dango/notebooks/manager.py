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
from datetime import datetime, timezone
from pathlib import Path

from dango.notebooks.access_token import (
    create_token_file,
    open_private_log,
    remove_token_file,
    start_lock,
)
from dango.notebooks.marimo_process import (
    _stop_marimo_process as _stop_marimo_process,
)
from dango.notebooks.marimo_process import (
    _stop_unready_process as _stop_unready_process,
)
from dango.notebooks.protection import ensure_marimo_protected as ensure_marimo_protected
from dango.utils.dango_db import connect
from dango.utils.process import (
    is_process_running,
    read_pid_record,
    write_pid_record,
)

logger = logging.getLogger(__name__)

# --- Idle auto-shutdown state ---
_idle_checker_task: asyncio.Task[None] | None = None
_IDLE_CHECK_INTERVAL = 300  # 5 minutes
_IDLE_TIMEOUT_LOCAL = 7200  # 2 hours
_IDLE_TIMEOUT_CLOUD = 3600  # 1 hour
_IDLE_TIMEOUT = _IDLE_TIMEOUT_LOCAL  # backward compat for smoke test

# Upper bound for Marimo to answer its readiness probe after spawn.
_MARIMO_START_TIMEOUT_SECONDS = 60
_CLOUD_BASE_PATH = "/notebooks/marimo"


def _get_idle_timeout(project_root: Path) -> int:  # noqa: ARG001
    """Return the idle timeout in seconds based on deployment mode."""
    from dango.config.helpers import is_running_on_cloud

    if is_running_on_cloud():
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


def _is_marimo_responding(port: int, timeout: float = 1.0, base_path: str = "/") -> bool:
    """Check if a Marimo server is responding on localhost:port.

    Probes ``<base_path>/health``, which Marimo leaves public even when a
    token is required (the root page redirects to a login form), and accepts
    HTTP 200 whose body reports ``healthy`` (not an unrelated service).
    Cloud runs Marimo under ``--base-url /notebooks/marimo``.
    """
    import urllib.request

    path = base_path.rstrip("/") + "/health"
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", errors="ignore")
            return bool(resp.status == 200 and "healthy" in body.lower())
    except Exception:
        return False


def start_marimo(
    project_root: Path,
    port: int | None = None,
    snapshot_path: Path | None = None,
) -> int | None:
    """Start the Marimo notebook server in the background; return its PID.

    Starts are serialized by a file lock.  *port* defaults to
    ``PlatformSettings.marimo_port``; *snapshot_path* becomes
    ``DANGO_NOTEBOOK_DB_PATH``.  Raises ``RuntimeError`` if Marimo is already
    running or fails to start.
    """
    with start_lock(project_root):
        return _start_marimo_locked(project_root, port, snapshot_path)


def _start_marimo_locked(
    project_root: Path, port: int | None, snapshot_path: Path | None
) -> int | None:
    """Body of :func:`start_marimo`, run while holding the start lock."""
    import sys

    pid_file = get_marimo_pid_file_path(project_root)
    if pid_file.exists():
        existing = read_pid_record(pid_file)
        if existing is not None and is_process_running(
            existing.pid, expected_start_time=existing.start_time
        ):
            raise RuntimeError(
                f"Marimo is already running (PID {existing.pid}).\nStop it with 'dango stop'"
            )
        pid_file.unlink(missing_ok=True)

    if port is None:
        from dango.config.loader import ConfigLoader

        loader = ConfigLoader(project_root)
        config = loader.load_config()
        port = config.platform.marimo_port

    notebooks_dir = project_root / "notebooks"
    notebooks_dir.mkdir(parents=True, exist_ok=True)

    log_file = project_root / ".dango" / "marimo.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Marimo's --timeout matches Dango's idle checker (fallback if it fails).
    timeout_minutes = _get_idle_timeout(project_root) // 60

    env = os.environ.copy()
    if snapshot_path is not None:
        env["DANGO_NOTEBOOK_DB_PATH"] = str(snapshot_path)

    log_handle = None
    try:
        log_handle = open_private_log(log_file)

        cmd = [
            sys.executable,
            "-m",
            "marimo",
            "edit",
            "--headless",
            "--port",
            str(port),
            "--host",
            "127.0.0.1",
            "--timeout",
            str(timeout_minutes),
            "--skip-update-check",
        ]

        # Add base-url for cloud proxy so SPA assets use correct prefix
        from dango.config.helpers import is_running_on_cloud

        on_cloud = is_running_on_cloud()
        base_path = _CLOUD_BASE_PATH if on_cloud else "/"
        if on_cloud:
            # Cloud: Marimo is only reachable through Caddy forward_auth / the
            # dango-web proxy, both of which authenticate the session.
            cmd.append("--no-token")
        else:
            # Local: nothing in front of Marimo, so it requires a random
            # per-start token (0600 file, never argv).  --quiet (a global
            # option, before "edit") stops Marimo printing the token-bearing URL.
            cmd.insert(3, "--quiet")
            cmd.extend(["--token-password-file", str(create_token_file(project_root))])
        if base_path != "/":
            cmd.extend(["--base-url", base_path])

        cmd.append(str(notebooks_dir))

        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

        # Wait for Marimo to start responding (poll until ready, dead, or
        # the deadline).  A Marimo that never answers is stopped instead of
        # being polled forever from a shared worker thread.
        # Any exception (timeout, early exit, probe error, interrupt, pid file
        # write) stops the spawned process group: nothing runs untracked.
        deadline = time.monotonic() + _MARIMO_START_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline:
                time.sleep(1)
                if proc.poll() is not None:
                    raise RuntimeError(f"Marimo failed to start. Check logs at {log_file}")
                if _is_marimo_responding(port, timeout=1.0, base_path=base_path):
                    break
            else:
                raise RuntimeError(
                    f"Marimo did not become ready within {_MARIMO_START_TIMEOUT_SECONDS} s. "
                    f"Check logs at {log_file}"
                )
            write_pid_record(pid_file, proc.pid)
        except BaseException:
            _stop_unready_process(proc)
            remove_token_file(project_root)
            raise

        return proc.pid

    except RuntimeError:
        remove_token_file(project_root)
        raise
    except Exception as e:
        remove_token_file(project_root)
        raise RuntimeError(f"Failed to start Marimo: {e}") from e
    finally:
        if log_handle is not None:
            log_handle.close()


def stop_marimo(project_root: Path) -> bool:
    """Stop the Marimo server; ``True`` if stopped, ``False`` if not running or not stoppable.

    The pid file is kept when the process could not be stopped, so it stays tracked.
    """
    pid_file = get_marimo_pid_file_path(project_root)

    if not pid_file.exists():
        logger.debug("No Marimo PID file found")
        return False

    record = read_pid_record(pid_file)
    if record is None:
        logger.warning("Invalid Marimo PID file")
        pid_file.unlink(missing_ok=True)
        return False
    pid = record.pid

    if not is_process_running(pid, expected_start_time=record.start_time):
        logger.debug("Marimo PID %d is not running (stale PID file)", pid)
        pid_file.unlink(missing_ok=True)
        remove_token_file(project_root)
        return False

    logger.debug("Stopping Marimo (PID %d)", pid)

    try:
        outcome = _stop_marimo_process(pid, record.start_time)
    except Exception:
        logger.warning("Failed to stop Marimo process %d", pid, exc_info=True)
        return False
    if outcome == "foreign":
        logger.warning(
            "PID %d in %s is not a Marimo process this user can stop; removing the stale file",
            pid,
            pid_file,
        )
        pid_file.unlink(missing_ok=True)
        remove_token_file(project_root)
        return False
    if outcome != "stopped":
        logger.warning(
            "Failed to stop Marimo process %d; keeping %s so it stays tracked", pid, pid_file
        )
        return False

    try:
        pid_file.unlink()
    except OSError:
        pass
    remove_token_file(project_root)
    logger.debug("Marimo stopped")
    return True


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
            record = read_pid_record(pid_file)
            if record is not None and is_process_running(
                record.pid, expected_start_time=record.start_time
            ):
                status["running"] = True
                status["pid"] = record.pid

                from dango.config.loader import ConfigLoader

                try:
                    loader = ConfigLoader(project_root)
                    config = loader.load_config()
                    status["port"] = config.platform.marimo_port
                except Exception:
                    status["port"] = 7805
        except (ValueError, OSError):  # noqa: BLE001
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
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }
        )
    except Exception:  # noqa: BLE001
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
