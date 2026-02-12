"""dango/web/routes/sync.py

Source sync trigger endpoint and background sync task.
"""

import logging
import time
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException

from dango.validation import validate_date_string, validate_source_name
from dango.web.helpers import (
    append_log_entry,
    get_project_root,
    get_source_row_count,
    load_sources_config,
    save_sync_history_entry,
)
from dango.web.models import SyncRequest, SyncResponse
from dango.web.routes.websocket import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sync"])


@router.post("/api/sources/{source_name}/sync", response_model=SyncResponse)
async def trigger_sync(
    source_name: str, sync_request: SyncRequest, background_tasks: BackgroundTasks
) -> SyncResponse:
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

    # Start sync in background
    background_tasks.add_task(
        run_sync_task,
        source_name,
        sync_request.full_refresh,
        sync_request.start_date,
        sync_request.end_date,
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
    """Run sync task in background.

    This function imports and runs the dlt sync process, broadcasting updates via WebSocket
    """
    from dango.utils import DbtLock, DbtLockError

    start_time = time.time()
    sync_timestamp = datetime.now().isoformat()
    success = False
    error_message = None
    rows_processed = 0
    project_root = get_project_root()

    # Try to acquire lock before running sync (which includes dbt)
    lock = None
    try:
        lock = DbtLock(
            project_root=project_root,
            source="ui",
            operation=f"sync {source_name} (includes dbt run)",
        )
        lock.acquire()
    except DbtLockError as e:
        # Lock is held by another process - broadcast error and return
        error_msg = str(e).split("\n")[0]
        await ws_manager.broadcast(
            {
                "event": "sync_failed",
                "source": source_name,
                "message": error_msg,
                "timestamp": datetime.now().isoformat(),
            }
        )
        append_log_entry(
            {
                "timestamp": datetime.now().isoformat(),
                "level": "error",
                "source": source_name,
                "message": f"Sync blocked: {error_msg}",
            }
        )
        logger.warning(f"Could not acquire dbt lock for sync {source_name}: {e}")
        return

    try:
        from dango.config.helpers import load_config

        # Log sync start
        append_log_entry(
            {
                "timestamp": sync_timestamp,
                "level": "info",
                "source": source_name,
                "message": f"Starting sync for {source_name}",
            }
        )

        # Broadcast sync started
        await ws_manager.broadcast(
            {
                "event": "sync_started",
                "source": source_name,
                "message": f"Starting sync for {source_name}",
                "timestamp": sync_timestamp,
            }
        )

        # Load config and get source
        config = load_config(get_project_root())
        source_config = config.sources.get_source(source_name)

        if not source_config:
            raise ValueError(f"Source '{source_name}' not found in configuration")

        # Use the same run_sync function as CLI for consistent behavior
        # This ensures: data load -> dbt run -> docs generation -> Metabase sync
        from dango.ingestion import run_sync

        # Parse dates if provided (validate_date_string raises InvalidDateFormatError)
        start_date_obj = validate_date_string(start_date) if start_date else None
        end_date_obj = validate_date_string(end_date) if end_date else None

        # Log before running sync
        append_log_entry(
            {
                "timestamp": datetime.now().isoformat(),
                "level": "info",
                "source": source_name,
                "message": "Loading data from source",
            }
        )

        # Run sync with the complete flow (data load -> dbt -> docs -> metabase)
        summary = run_sync(
            project_root=get_project_root(),
            sources=[source_config],
            start_date=start_date_obj,
            end_date=end_date_obj,
            full_refresh=full_refresh,
        )

        success = summary["failed_count"] == 0
        # Also check that we actually have successful sources (not just zero failures)
        has_successful_sources = len(summary.get("success_sources", [])) > 0

        # Extract error message if sync failed
        if not success and summary.get("failed_sources"):
            # Get error from first failed source (there should only be one when syncing single source)
            error_message = summary["failed_sources"][0].get("error", "Unknown error")
        else:
            error_message = None

        # Log after data load
        append_log_entry(
            {
                "timestamp": datetime.now().isoformat(),
                "level": "success" if success else "error",
                "source": source_name,
                "message": f"Data load {'completed' if success else 'failed'}"
                + (f": {error_message}" if error_message else ""),
            }
        )

        # Broadcast and log dbt run (which happens inside run_sync AFTER data load)
        # ONLY broadcast dbt messages if sync actually succeeded AND we have successful sources
        if success and has_successful_sources:
            # Build helpful message about what dbt will run
            dbt_message = f"Running dbt models for source: {source_name}"
            dbt_detail = f"Processing staging.{source_name} and downstream models"

            # Broadcast dbt started (happens after data load, before dbt actually runs in run_sync)
            await ws_manager.broadcast(
                {
                    "event": "dbt_run_all_started",
                    "source": f"dbt (triggered by {source_name})",
                    "message": dbt_message,
                    "timestamp": datetime.now().isoformat(),
                }
            )
            append_log_entry(
                {
                    "timestamp": datetime.now().isoformat(),
                    "level": "info",
                    "source": f"dbt (triggered by {source_name})",
                    "message": dbt_detail,
                }
            )

            append_log_entry(
                {
                    "timestamp": datetime.now().isoformat(),
                    "level": "success",
                    "source": f"dbt (triggered by {source_name})",
                    "message": f"dbt models completed: staging.{source_name} and downstream",
                }
            )

            # Broadcast dbt run completed
            await ws_manager.broadcast(
                {
                    "event": "dbt_run_all_completed",
                    "source": f"dbt (triggered by {source_name})",
                    "message": f"dbt models completed for {source_name}",
                    "timestamp": datetime.now().isoformat(),
                }
            )

        # Get row count after sync (only if successful)
        if success:
            rows_processed = get_source_row_count(source_name) or 0

        # Calculate duration
        duration = time.time() - start_time

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

        # Log completion (conditional based on success/failure)
        if success:
            append_log_entry(
                {
                    "timestamp": datetime.now().isoformat(),
                    "level": "success",
                    "source": source_name,
                    "message": f"Sync completed in {round(duration, 1)}s - {rows_processed:,} rows",
                }
            )

            # Trigger Metabase schema sync to ensure new tables are discoverable
            # This matches CLI behavior (main.py:1265-1275) which calls sync_metabase_schema
            # after run_sync() as a backup in case the internal call was skipped
            from dango.visualization.metabase import sync_metabase_schema

            sync_metabase_schema(project_root)
        else:
            # For failures, log with error details
            append_log_entry(
                {
                    "timestamp": datetime.now().isoformat(),
                    "level": "error",
                    "source": source_name,
                    "message": f"Sync failed after {round(duration, 1)}s"
                    + (f": {error_message}" if error_message else ""),
                }
            )

        # Broadcast completion with detailed error message if failed
        await ws_manager.broadcast(
            {
                "event": "sync_completed" if success else "sync_failed",
                "source": source_name,
                "message": "Sync completed successfully"
                if success
                else (error_message or "Sync failed"),
                "timestamp": datetime.now().isoformat(),
                "error": error_message if not success else None,
            }
        )

    except Exception as e:
        logger.error(f"Error running sync for {source_name}: {e}")
        error_message = str(e)

        # Log error
        append_log_entry(
            {
                "timestamp": datetime.now().isoformat(),
                "level": "error",
                "source": source_name,
                "message": f"Sync failed: {error_message}",
            }
        )

        # Calculate duration
        duration = time.time() - start_time

        # Save failed sync history
        history_entry = {
            "timestamp": sync_timestamp,
            "status": "failed",
            "duration_seconds": round(duration, 2),
            "rows_processed": 0,
            "full_refresh": full_refresh,
            "error_message": error_message,
        }
        save_sync_history_entry(source_name, history_entry)

        # Broadcast error
        await ws_manager.broadcast(
            {
                "event": "sync_failed",
                "source": source_name,
                "message": f"Sync failed: {error_message}",
                "timestamp": datetime.now().isoformat(),
            }
        )
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
