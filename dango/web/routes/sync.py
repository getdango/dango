"""dango/web/routes/sync.py

Source sync trigger endpoint, background sync task, and remote sync trigger API.

Syncs run in a subprocess via sync_process.py — the web server process never
holds the DuckDB write lock, so notebooks and the UI remain responsive.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.logging import get_logger
from dango.platform.scheduling.history import (
    get_execution_record,
    get_scheduler_db_path,
    record_completion,
    record_failure,
    record_start,
)
from dango.validation import (
    parse_backfill_duration,
    validate_date_string,
    validate_source_name,
)
from dango.web.helpers import (
    append_log_entry,
    get_project_root,
    get_source_row_count,
    load_sources_config,
    save_sync_history_entry,
)
from dango.web.models import SyncRequest, SyncResponse, SyncTriggerRequest
from dango.web.routes.websocket import ws_manager

_logger = get_logger(__name__)

router = APIRouter(tags=["sync"])


@router.post("/api/sources/{source_name}/sync", response_model=SyncResponse)
async def trigger_sync(source_name: str, sync_request: SyncRequest) -> SyncResponse:
    """Trigger sync for a specific source.

    Args:
        source_name: Name of the source to sync
        sync_request: Sync parameters (full_refresh, date range)

    Returns:
        Sync response with status
    """
    source_name = validate_source_name(source_name)
    if sync_request.start_date:
        validate_date_string(sync_request.start_date)
    if sync_request.end_date:
        validate_date_string(sync_request.end_date)

    # Verify source exists
    sources_config = load_sources_config()
    source_exists = any(s.get("name") == source_name for s in sources_config)

    if not source_exists:
        raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")

    # Start sync in background — use asyncio.create_task so the async
    # coroutine runs on the event loop (WebSocket broadcasts work correctly).
    asyncio.create_task(
        run_sync_task(
            source_name,
            sync_request.full_refresh,
            sync_request.start_date,
            sync_request.end_date,
        )
    )

    # Broadcast sync started event via WebSocket
    await ws_manager.broadcast(
        {"event": "sync_started", "source": source_name, "timestamp": datetime.now().isoformat()}
    )

    return SyncResponse(
        success=True,
        message=f"Sync started for {source_name}",
        source_name=source_name,
        started_at=datetime.now().isoformat(),
    )


async def run_sync_task(
    source_name: str, full_refresh: bool, start_date: str | None, end_date: str | None
) -> None:
    """Run sync task in a subprocess, polling for status and broadcasting updates."""
    from dango.platform.sync_process import (
        cleanup_sync_status,
        launch_sync_subprocess,
        poll_sync_status,
    )

    start_time = time.time()
    sync_timestamp = datetime.now().isoformat()
    project_root = get_project_root()

    # Immediate UI feedback
    await ws_manager.broadcast(
        {
            "event": "sync_started",
            "source": source_name,
            "message": f"Starting sync for {source_name}",
            "timestamp": sync_timestamp,
        }
    )
    append_log_entry(
        {
            "timestamp": sync_timestamp,
            "level": "info",
            "source": source_name,
            "message": f"Starting sync for {source_name}",
        }
    )

    try:
        # Launch subprocess (DbtLock acquired inside subprocess)
        process = launch_sync_subprocess(
            project_root=project_root,
            sources=[source_name],
            full_refresh=full_refresh,
            start_date=start_date,
            end_date=end_date,
            source_label="ui",
        )

        # Poll until completion (broadcasts WS events + heartbeat internally)
        success, result = await poll_sync_status(project_root, process, source_name)

        # Calculate duration
        duration = time.time() - start_time
        error_message = result.get("error") if result else None

        # Get row count after sync (only if successful)
        rows_processed = 0
        if success:
            rows_processed = get_source_row_count(source_name) or 0

        # Save sync history
        history_entry = {
            "timestamp": sync_timestamp,
            "status": "success" if success else "failed",
            "duration_seconds": round(duration, 2),
            "rows_processed": rows_processed if success else 0,
            "full_refresh": full_refresh,
            "error_message": error_message,
        }
        save_sync_history_entry(source_name, history_entry)

        # Log completion
        if success:
            append_log_entry(
                {
                    "timestamp": datetime.now().isoformat(),
                    "level": "success",
                    "source": source_name,
                    "message": f"Sync completed in {round(duration, 1)}s - {rows_processed:,} rows",
                }
            )
        else:
            append_log_entry(
                {
                    "timestamp": datetime.now().isoformat(),
                    "level": "error",
                    "source": source_name,
                    "message": f"Sync failed after {round(duration, 1)}s"
                    + (f": {error_message}" if error_message else ""),
                }
            )

        # Cleanup status file
        cleanup_sync_status(project_root)

    except Exception as e:
        _logger.warning("sync_task_error", source=source_name, error=str(e), exc_info=True)
        error_message = str(e)

        duration = time.time() - start_time
        append_log_entry(
            {
                "timestamp": datetime.now().isoformat(),
                "level": "error",
                "source": source_name,
                "message": f"Sync failed: {error_message}",
            }
        )

        history_entry = {
            "timestamp": sync_timestamp,
            "status": "failed",
            "duration_seconds": round(duration, 2),
            "rows_processed": 0,
            "full_refresh": full_refresh,
            "error_message": error_message,
        }
        save_sync_history_entry(source_name, history_entry)

        await ws_manager.broadcast(
            {
                "event": "sync_failed",
                "source": source_name,
                "message": f"Sync failed: {error_message}",
                "timestamp": datetime.now().isoformat(),
            }
        )


# ---------------------------------------------------------------------------
# Remote sync trigger API (TASK-040c)
# ---------------------------------------------------------------------------


@router.post("/api/sync/trigger")
async def trigger_manual_sync(
    request: Request,
    body: SyncTriggerRequest,
    user: User = Depends(require_permission("source.sync")),
) -> JSONResponse:
    """Trigger a manual sync for one or more sources with execution history tracking.

    Returns a ``job_id`` that can be polled via ``GET /api/sync/status/{job_id}``.
    """
    project_root = get_project_root()

    # Validate sources exist
    sources_config = load_sources_config()
    known_names = {s.get("name") for s in sources_config}
    for src in body.sources:
        if src not in known_names:
            return JSONResponse(
                status_code=404,
                content={
                    "error_code": "DANGO-SYNC-001",
                    "message": f"Source {src!r} not found.",
                },
            )

    # Parse backfill duration
    backfill_days: int | None = None
    if body.backfill is not None:
        try:
            backfill_days = parse_backfill_duration(body.backfill)
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content={
                    "error_code": "DANGO-SYNC-002",
                    "message": str(exc),
                },
            )

    # Create execution history record
    db_path = get_scheduler_db_path(project_root)
    job_id = record_start(db_path, "manual", sources=body.sources)

    # Launch background sync via subprocess
    asyncio.create_task(
        _run_manual_sync(
            project_root,
            body.sources,
            body.full_refresh,
            backfill_days,
            job_id,
            db_path,
        )
    )

    # Audit log
    log_auth_event(
        AuditEvent.SYNC_TRIGGERED,
        user_id=user.id,
        email=user.email,
        ip=request.client.host if request.client else None,
        details={
            "sources": body.sources,
            "full_refresh": body.full_refresh,
            "backfill": body.backfill,
            "job_id": job_id,
        },
    )

    return JSONResponse(
        content={
            "job_id": job_id,
            "sources": body.sources,
            "status": "started",
        },
    )


async def _run_manual_sync(
    project_root: Any,
    source_names: list[str],
    full_refresh: bool,
    backfill_days: int | None,
    record_id: int,
    db_path: Any,
) -> None:
    """Background task that executes a manual sync in a subprocess and records the result."""
    from dango.platform.sync_process import (
        cleanup_sync_status,
        launch_sync_subprocess,
        poll_sync_status,
    )

    try:
        process = launch_sync_subprocess(
            project_root=project_root,
            sources=source_names,
            full_refresh=full_refresh,
            backfill_days=backfill_days,
            source_label="manual",
        )

        # Use first source name for WS events (manual sync may have multiple)
        display_name = source_names[0] if source_names else "manual"
        success, _result = await poll_sync_status(project_root, process, display_name)

        if success:
            record_completion(db_path, record_id)
        else:
            error = _result.get("error", "Unknown error") if _result else "Unknown error"
            record_failure(db_path, record_id, error)

        cleanup_sync_status(project_root)
    except Exception as exc:
        _logger.warning("manual_sync_failed", error=str(exc), exc_info=True)
        record_failure(db_path, record_id, str(exc))


@router.get("/api/sync/status/{record_id}")
async def get_sync_status(
    record_id: int,
    user: User = Depends(require_permission("source.sync")),
) -> JSONResponse:
    """Poll execution status for a manual sync job."""
    project_root = get_project_root()
    db_path = get_scheduler_db_path(project_root)
    record = get_execution_record(db_path, record_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-SYNC-003",
                "message": f"Execution record {record_id} not found.",
            },
        )
    return JSONResponse(content=record)
