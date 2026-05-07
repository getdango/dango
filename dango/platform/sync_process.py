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
) -> tuple[subprocess.Popen, str]:
    """Spawn sync subprocess via sys.executable.

    Returns (Popen handle, sync_id). The sync_id uniquely identifies the
    status file so concurrent syncs don't clobber each other.

    The subprocess runs ``python -m dango.platform.scheduling.sync_trigger``
    with write_progress=True.
    """
    sync_id = uuid4().hex[:12]

    # Clean up any stale status file for the default path (legacy)
    cleanup_sync_status(project_root)

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

    process = subprocess.Popen(
        [sys.executable, "-m", "dango.platform.scheduling.sync_trigger", json_args],
        cwd=str(project_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    logger.info(
        "sync_subprocess_launched",
        pid=process.pid,
        sources=sources,
        source_label=source_label,
        sync_id=sync_id,
    )
    return process, sync_id


async def poll_sync_status(
    project_root: Path,
    process: subprocess.Popen,
    source_name: str,
    sync_id: str | None = None,
    poll_interval: float = 2.0,
    heartbeat_interval: float = 30.0,
    max_poll_time: float = 3600.0,
) -> tuple[bool, dict[str, Any] | None]:
    """Async poll — reads status file, broadcasts WS events on transitions.

    Emits heartbeat every ``heartbeat_interval`` seconds. Detects subprocess
    crash via ``process.poll()``. Times out after ``max_poll_time`` seconds.
    Returns (success, result_dict).
    """
    from dango.web.routes.websocket import ws_manager

    last_phase: str | None = None
    last_heartbeat = time.time()
    start_time = time.time()

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
            return False, {"error": error_msg, "phase": "failed"}

        # Check if subprocess has exited
        exit_code = process.poll()

        # Read current status
        status = read_sync_status(project_root, sync_id)

        if status is not None:
            current_phase = status.get("phase", "")

            # Broadcast on phase transitions
            if current_phase != last_phase:
                await _broadcast_phase_transition(ws_manager, source_name, current_phase, status)
                last_phase = current_phase

            # Detect terminal state
            if current_phase in ("completed", "failed"):
                return current_phase == "completed", status

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
            await ws_manager.broadcast(
                {
                    "event": "sync_failed",
                    "source": source_name,
                    "message": error_msg,
                    "timestamp": _ts(),
                }
            )
            return False, {"error": error_msg, "phase": "failed"}


def poll_sync_status_blocking(
    project_root: Path,
    process: subprocess.Popen,
    source_name: str = "",
    sync_id: str | None = None,
    broadcast_fn: Callable[[dict[str, Any]], None] | None = None,
    poll_interval: float = 2.0,
    max_poll_time: float = 3600.0,
) -> tuple[bool, dict[str, Any] | None]:
    """Synchronous version for APScheduler thread pool. Returns (success, result_dict).

    Uses time.sleep() for polling. Calls optional broadcast_fn on phase transitions.
    """
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
            return False, {"error": error_msg, "phase": "failed"}

        exit_code = process.poll()
        status = read_sync_status(project_root, sync_id)

        if status is not None:
            current_phase = status.get("phase", "")

            # Broadcast on phase transitions
            if current_phase != last_phase and broadcast_fn is not None:
                broadcast_fn(
                    {
                        "event": _phase_to_event(current_phase),
                        "source": source_name,
                        "phase": current_phase,
                        "message": status.get("message", ""),
                        "timestamp": _ts(),
                    }
                )
                last_phase = current_phase

            # Detect terminal state
            if current_phase in ("completed", "failed"):
                success = current_phase == "completed"
                return success, status

        # Process crashed without writing final status
        if exit_code is not None and (
            status is None or status.get("phase") not in ("completed", "failed")
        ):
            error_msg = f"Sync process terminated unexpectedly (exit code {exit_code})"
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
    elif phase == "completed":
        message["rows_loaded"] = status.get("rows_loaded", 0)
        message["duration_seconds"] = status.get("elapsed_seconds", 0)
    elif phase == "failed":
        message["error"] = status.get("error")

    await ws_manager.broadcast(message)
