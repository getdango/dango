"""dango/platform/sync_process.py

Subprocess launch and polling utilities for process-isolated syncs.

The web server and scheduler use these functions to run sync operations
in a subprocess, keeping the DuckDB write lock out of the web server process.
This allows notebooks and the web UI to coexist without lock conflicts.

Public API:
    get_sync_status_path(project_root, sync_id) -- path to sync status file
    read_sync_status(project_root, sync_id)     -- read current status (or None)
    cleanup_sync_status(project_root, sync_id)  -- remove stale status file
    launch_sync_subprocess(...)                  -- spawn sync subprocess
    poll_sync_status(...)                        -- async poll with WS broadcasts
    poll_sync_status_blocking(...)               -- sync poll for scheduler threads
    start_sync_status_watcher(project_root)      -- background watcher for multi-worker
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from dango.logging import get_logger

logger = get_logger(__name__)

# Sync IDs being actively polled by this worker (skip in background watcher)
_locally_polled: set[str] = set()


def get_sync_status_path(project_root: Path, sync_id: str | None = None) -> Path:
    """Return .dango/state/sync_status_{sync_id}.json path."""
    filename = f"sync_status_{sync_id}.json" if sync_id else "sync_status.json"
    return project_root / ".dango" / "state" / filename


def read_sync_status(project_root: Path, sync_id: str | None = None) -> dict[str, Any] | None:
    """Read current sync status file. Returns None if missing or unparseable."""
    path = get_sync_status_path(project_root, sync_id)
    try:
        if not path.exists():
            return None
        with open(path) as f:
            result: dict[str, Any] = json.load(f)
            return result
    except (json.JSONDecodeError, OSError):
        return None


def _is_pid_alive(pid: int) -> bool:
    """Check if a PID is alive (cross-platform)."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def cleanup_sync_status(project_root: Path, sync_id: str | None = None) -> None:
    """Remove stale status file (dead PID or completed).

    Safe to call any time — only removes the file if the referenced
    process is no longer running or the sync has a terminal phase.
    """
    status = read_sync_status(project_root, sync_id)
    if status is None:
        return

    pid = status.get("pid")
    phase = status.get("phase", "")
    terminal_phases = {"completed", "failed"}

    if phase in terminal_phases or (pid is not None and not _is_pid_alive(pid)):
        path = get_sync_status_path(project_root, sync_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def launch_sync_subprocess(
    project_root: Path,
    sources: list[str],
    full_refresh: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
    backfill_days: int | None = None,
    skip_dbt: bool = False,
    source_label: str = "ui",
    max_lock_wait: int = 0,
    record_id: int | None = None,
) -> tuple[subprocess.Popen, str, Path]:
    """Spawn sync subprocess via sys.executable.

    Returns (Popen handle, sync_id, log_path). The sync_id uniquely identifies
    the status file so concurrent syncs don't clobber each other. The log_path
    captures stdout+stderr so crash output is available for diagnostics.

    The subprocess runs ``python -m dango.platform.scheduling.sync_trigger``
    with write_progress=True.
    """
    sync_id = uuid4().hex[:12]

    # Clean up any stale status file for the default path (legacy)
    cleanup_sync_status(project_root)

    # Capture subprocess output to a log file (was DEVNULL — silent crashes)
    log_dir = project_root / ".dango" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"sync_{sync_id}.log"

    args_dict: dict[str, Any] = {
        "project_root": str(project_root),
        "sources": sources,
        "full_refresh": full_refresh,
        "write_progress": True,
        "source_label": source_label,
        "skip_dbt": skip_dbt,
        "max_lock_wait": max_lock_wait,
        "sync_id": sync_id,
    }
    if start_date is not None:
        args_dict["start_date"] = start_date
    if end_date is not None:
        args_dict["end_date"] = end_date
    if backfill_days is not None:
        args_dict["backfill_days"] = backfill_days
    if record_id is not None:
        args_dict["record_id"] = record_id

    json_args = json.dumps(args_dict)

    log_handle = open(log_path, "w")  # noqa: SIM115
    process = subprocess.Popen(
        [sys.executable, "-m", "dango.platform.scheduling.sync_trigger", json_args],
        cwd=str(project_root),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    # Close the handle in the parent process — the child has its own fd copy
    log_handle.close()

    logger.info(
        "sync_subprocess_launched",
        pid=process.pid,
        sources=sources,
        source_label=source_label,
        sync_id=sync_id,
        log_path=str(log_path),
    )
    return process, sync_id, log_path


async def poll_sync_status(
    project_root: Path,
    process: subprocess.Popen,
    source_name: str,
    sync_id: str | None = None,
    poll_interval: float = 2.0,
    heartbeat_interval: float = 30.0,
    max_poll_time: float = 3600.0,
    log_path: Path | None = None,
    sources: list[str] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Async poll — reads status file, broadcasts WS events on transitions.

    Emits heartbeat every ``heartbeat_interval`` seconds. Detects subprocess
    crash via ``process.poll()``. Times out after ``max_poll_time`` seconds.
    Returns (success, result_dict).

    When *log_path* is provided, crash output is read from the file and
    written to activity log + sync history.  On success the log file is
    deleted; on failure it is kept for 7 days of diagnostics.
    """
    from dango.web.routes.websocket import ws_manager

    if sync_id:
        _locally_polled.add(sync_id)

    source_list = sources or ([source_name] if source_name else [])
    last_phase: str | None = None
    last_heartbeat = time.time()
    start_time = time.time()

    try:
        while True:
            await asyncio.sleep(poll_interval)

            # Check timeout
            elapsed = time.time() - start_time
            if elapsed >= max_poll_time:
                process.terminate()
                error_msg = f"Sync timed out after {int(elapsed)}s"
                await ws_manager.broadcast(
                    {
                        "event": "sync_failed",
                        "source": source_name,
                        "message": error_msg,
                        "timestamp": _ts(),
                    }
                )
                # Timeout is not a crash — don't call _handle_crash (the caller
                # has its own timeout handling). Just keep the log for diagnostics.
                if log_path:
                    _cleanup_sync_log(log_path, keep=True)
                return False, {"error": error_msg, "phase": "failed"}

            # Check if subprocess has exited
            exit_code = process.poll()

            # Read current status
            status = read_sync_status(project_root, sync_id)

            if status is not None:
                current_phase = status.get("phase", "")

                # Broadcast on phase transitions
                if current_phase != last_phase:
                    await _broadcast_phase_transition(
                        ws_manager, source_name, current_phase, status
                    )
                    last_phase = current_phase

                # Detect terminal state
                if current_phase in ("completed", "failed"):
                    success = current_phase == "completed"
                    if log_path:
                        _cleanup_sync_log(log_path, keep=not success)
                    return success, status

            # Heartbeat
            now = time.time()
            if now - last_heartbeat >= heartbeat_interval:
                elapsed_int = int(now - start_time)
                await ws_manager.broadcast(
                    {
                        "event": "sync_progress",
                        "source": source_name,
                        "message": f"Sync in progress ({elapsed_int}s elapsed)",
                        "timestamp": _ts(),
                    }
                )
                last_heartbeat = now

            # Process crashed without writing final status
            if exit_code is not None and (
                status is None or status.get("phase") not in ("completed", "failed")
            ):
                error_msg = f"Sync process terminated unexpectedly (exit code {exit_code})"
                _handle_crash(project_root, source_list, exit_code, log_path, error_msg)
                if log_path:
                    _cleanup_sync_log(log_path, keep=True)
                await ws_manager.broadcast(
                    {
                        "event": "sync_failed",
                        "source": source_name,
                        "message": error_msg,
                        "timestamp": _ts(),
                    }
                )
                return False, {"error": error_msg, "phase": "failed"}
    finally:
        if sync_id:
            _locally_polled.discard(sync_id)


def poll_sync_status_blocking(
    project_root: Path,
    process: subprocess.Popen,
    source_name: str = "",
    sync_id: str | None = None,
    broadcast_fn: Callable[[dict[str, Any]], None] | None = None,
    poll_interval: float = 2.0,
    max_poll_time: float = 3600.0,
    log_path: Path | None = None,
    sources: list[str] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Synchronous version for APScheduler thread pool. Returns (success, result_dict).

    Uses time.sleep() for polling. Calls optional broadcast_fn on phase transitions.
    When *log_path* is provided, crash output is captured and written to
    activity log + sync history.
    """
    source_list = sources or ([source_name] if source_name else [])
    last_phase: str | None = None
    start_time = time.time()

    while True:
        time.sleep(poll_interval)

        # Check timeout
        elapsed = time.time() - start_time
        if elapsed >= max_poll_time:
            process.terminate()
            error_msg = f"Sync timed out after {int(elapsed)}s"
            if broadcast_fn is not None:
                broadcast_fn(
                    {
                        "event": "sync_failed",
                        "source": source_name,
                        "message": error_msg,
                        "timestamp": _ts(),
                    }
                )
            # Timeout is not a crash — don't call _handle_crash (the caller
            # has its own timeout handling). Just keep the log for diagnostics.
            if log_path:
                _cleanup_sync_log(log_path, keep=True)
            return False, {"error": error_msg, "phase": "failed"}

        exit_code = process.poll()
        status = read_sync_status(project_root, sync_id)

        if status is not None:
            current_phase = status.get("phase", "")

            # Broadcast on phase transitions
            if current_phase != last_phase and broadcast_fn is not None:
                msg = {
                    "event": _phase_to_event(current_phase),
                    "source": source_name,
                    "phase": current_phase,
                    "message": status.get("message", ""),
                    "timestamp": _ts(),
                }
                if current_phase == "failed" and status.get("dbt_error"):
                    msg["event"] = "dbt_run_all_failed"
                    msg["source"] = f"dbt (triggered by {source_name})"
                broadcast_fn(msg)
                last_phase = current_phase

            # Detect terminal state
            if current_phase in ("completed", "failed"):
                success = current_phase == "completed"
                if log_path:
                    _cleanup_sync_log(log_path, keep=not success)
                return success, status

        # Process crashed without writing final status
        if exit_code is not None and (
            status is None or status.get("phase") not in ("completed", "failed")
        ):
            error_msg = f"Sync process terminated unexpectedly (exit code {exit_code})"
            _handle_crash(project_root, source_list, exit_code, log_path, error_msg)
            if log_path:
                _cleanup_sync_log(log_path, keep=True)
            if broadcast_fn is not None:
                broadcast_fn(
                    {
                        "event": "sync_failed",
                        "source": source_name,
                        "message": error_msg,
                        "timestamp": _ts(),
                    }
                )
            return False, {"error": error_msg, "phase": "failed"}


# ---------------------------------------------------------------------------
# Crash handling helpers
# ---------------------------------------------------------------------------


def _read_log_tail(log_path: Path, max_lines: int = 50) -> str:
    """Read the last *max_lines* of a sync log file.  Returns '' on any error."""
    try:
        if not log_path.exists():
            return ""
        lines = log_path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:  # noqa: BLE001
        return ""


def _cleanup_sync_log(log_path: Path, *, keep: bool) -> None:
    """Delete or keep a sync log file depending on outcome."""
    try:
        if not keep and log_path.exists():
            log_path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _handle_crash(
    project_root: Path,
    sources: list[str],
    exit_code: int | None,
    log_path: Path | None,
    error_msg: str,
) -> None:
    """Write crash information to activity log and sync history.

    Called when a sync subprocess terminates without writing a terminal status.
    """
    from dango.utils.activity_log import log_activity
    from dango.utils.sync_history import load_sync_history, save_sync_history_entry

    log_tail = _read_log_tail(log_path) if log_path else ""
    detail = f"{error_msg}\n{log_tail}".strip() if log_tail else error_msg

    for src in sources:
        try:
            # Skip if the subprocess already wrote a terminal history entry
            # (e.g., it crashed after recording its own failure in dlt_runner)
            recent = load_sync_history(project_root, src, limit=1)
            if recent and recent[0].get("status") in ("failed", "success", "partial"):
                # Check if this entry is recent (within last 5 minutes)
                from datetime import datetime, timezone

                entry_ts = recent[0].get("timestamp", "")
                try:
                    clean = entry_ts.replace("+00:00", "").replace("Z", "")
                    ts = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
                    age = (datetime.now(tz=timezone.utc) - ts).total_seconds()
                    if age < 300:  # 5 minutes
                        continue  # subprocess already recorded this
                except (ValueError, TypeError):
                    pass  # can't parse — write our own entry

            save_sync_history_entry(
                project_root,
                src,
                {
                    "timestamp": _ts(),
                    "status": "failed",
                    "duration_seconds": 0,
                    "rows_processed": 0,
                    "error_message": detail[:2000],  # cap length
                },
            )
        except Exception:  # noqa: BLE001
            pass

    try:
        source_label = ", ".join(sources) if sources else "unknown"
        log_activity(project_root, "error", source_label, f"Sync crashed: {detail[:1000]}")
    except Exception:  # noqa: BLE001
        pass

    # Mark downstream dbt models as stale
    try:
        from dango.utils.dbt_status import mark_source_models_stale

        mark_source_models_stale(project_root, sources)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> str:
    """UTC ISO timestamp."""
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat()


def _phase_to_event(phase: str) -> str:
    """Map status file phase to a WebSocket event name."""
    mapping = {
        "starting": "sync_started",
        "lock_waiting": "sync_progress",
        "data_load": "sync_progress",
        "data_load_complete": "data_load_complete",
        "dbt_started": "dbt_run_all_started",
        "dbt_complete": "dbt_run_all_completed",
        "dbt_failed": "dbt_run_all_failed",
        "completed": "sync_completed",
        "failed": "sync_failed",
    }
    return mapping.get(phase, "sync_progress")


async def _broadcast_phase_transition(
    ws_manager: Any,
    source_name: str,
    phase: str,
    status: dict[str, Any],
) -> None:
    """Broadcast a WebSocket event for a phase transition."""
    event = _phase_to_event(phase)
    message: dict[str, Any] = {
        "event": event,
        "source": source_name,
        "message": status.get("message", ""),
        "timestamp": _ts(),
    }

    # Add extra fields based on phase
    if phase == "data_load_complete":
        message["rows_loaded"] = status.get("rows_loaded", 0)
    elif phase == "dbt_started":
        message["source"] = f"dbt (triggered by {source_name})"
    elif phase == "dbt_complete":
        message["source"] = f"dbt (triggered by {source_name})"
    elif phase == "dbt_failed":
        message["source"] = f"dbt (triggered by {source_name})"
    elif phase == "completed":
        message["rows_loaded"] = status.get("rows_loaded", 0)
        message["duration_seconds"] = status.get("elapsed_seconds", 0)
    elif phase == "failed":
        if status.get("dbt_error"):
            message["event"] = "dbt_run_all_failed"
            message["source"] = f"dbt (triggered by {source_name})"
        message["error"] = status.get("error")

    await ws_manager.broadcast(message, log=False)


# ---------------------------------------------------------------------------
# Background sync status watcher (multi-worker support)
# ---------------------------------------------------------------------------


async def start_sync_status_watcher(
    project_root: Path,
    poll_interval: float = 2.0,
) -> asyncio.Task:
    """Start a background asyncio task that watches sync status files.

    In a multi-worker uvicorn deployment, only the worker that triggered
    a sync runs ``poll_sync_status()``.  Other workers need this watcher
    to pick up status-file changes and broadcast them to their own
    WebSocket clients.

    Returns the ``asyncio.Task`` so the caller can cancel it on shutdown.
    """
    task = asyncio.create_task(
        _sync_status_watcher_loop(project_root, poll_interval),
        name="sync_status_watcher",
    )
    logger.debug("sync_status_watcher_started", project_root=str(project_root))
    return task


async def _sync_status_watcher_loop(
    project_root: Path,
    poll_interval: float,
) -> None:
    """Continuously scan status files and broadcast phase transitions.

    Skips sync_ids in ``_locally_polled`` (already handled by
    ``poll_sync_status()`` in this worker).  Uses file mtime to avoid
    re-broadcasting unchanged states.
    """
    from dango.web.routes.websocket import ws_manager

    state_dir = project_root / ".dango" / "state"
    # Track (last_phase, last_mtime) per sync_id
    known_states: dict[str, tuple[str, float]] = {}

    while True:
        try:
            await asyncio.sleep(poll_interval)

            if not state_dir.exists():
                continue

            # Discover active status files
            seen_ids: set[str] = set()
            for path in state_dir.iterdir():
                if not path.name.startswith("sync_status_") or not path.name.endswith(".json"):
                    continue

                # Extract sync_id from filename
                # sync_status_{sync_id}.json → sync_id
                sync_id = path.name[len("sync_status_") : -len(".json")]
                if not sync_id:
                    continue

                seen_ids.add(sync_id)

                # Skip if this worker is already polling this sync
                if sync_id in _locally_polled:
                    continue

                # Check mtime for efficiency
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue

                prev = known_states.get(sync_id)
                if prev is not None and prev[1] == mtime:
                    continue  # file unchanged

                # Read and broadcast
                status = read_sync_status(project_root, sync_id)
                if status is None:
                    continue

                current_phase = status.get("phase", "")
                source_name = ",".join(status.get("sources", ["unknown"]))

                prev_phase = prev[0] if prev else None
                if current_phase != prev_phase:
                    await _broadcast_phase_transition(
                        ws_manager, source_name, current_phase, status
                    )

                known_states[sync_id] = (current_phase, mtime)

            # Remove tracking for files that have disappeared
            stale_ids = set(known_states) - seen_ids
            for sid in stale_ids:
                known_states.pop(sid, None)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("sync_status_watcher_error", exc_info=True)
