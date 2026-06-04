"""dango/platform/scheduling/jobs.py

Module-level job functions for APScheduler pickle serialization.

APScheduler 3.x serializes job references by module path.  Functions must be
defined at module level (not as methods or lambdas) so they can be pickled
and restored from the SQLite job store across restarts.

Public API:
    configure_jobs(loop, scheduler)  -- set event loop + scheduler service
    run_scheduled_sync(...)          -- sync one or more data sources on schedule
    run_scheduled_dbt(...)           -- run dbt models on schedule
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dango.logging import get_logger

if TYPE_CHECKING:
    from dango.config.models import DataSource
    from dango.platform.scheduling.scheduler import SchedulerService

logger = get_logger(__name__)

_event_loop: asyncio.AbstractEventLoop | None = None
_scheduler_service: SchedulerService | None = None
_BROADCAST_TIMEOUT = 5  # seconds to wait for a WebSocket broadcast


def configure_jobs(
    loop: asyncio.AbstractEventLoop,
    scheduler: SchedulerService | None = None,
) -> None:
    """Store the running event loop and scheduler service for async bridging.

    Called once by ``SchedulerService.start()``.
    """
    global _event_loop, _scheduler_service  # noqa: PLW0603
    _event_loop = loop
    _scheduler_service = scheduler
    logger.info("scheduler_jobs_configured")


# ---------------------------------------------------------------------------
# Async bridge helpers
# ---------------------------------------------------------------------------


def _broadcast(message: dict[str, Any]) -> None:
    """Bridge a WebSocket broadcast from the thread-pool to the event loop.

    Silently returns on any error — broadcast failures must never block a job.
    """
    if _event_loop is None:
        return
    try:
        from dango.web.routes.websocket import ws_manager

        future = asyncio.run_coroutine_threadsafe(
            ws_manager.broadcast(message, log=False), _event_loop
        )
        future.result(timeout=_BROADCAST_TIMEOUT)
    except Exception:  # noqa: BLE001
        logger.debug("broadcast_bridge_error", exc_info=True)


def _notify(
    sender: Any,
    *,
    event_type: Any,
    schedule_name: str,
    sources: list[str] | None = None,
    error: str | None = None,
    duration_seconds: float | None = None,
    stale_hours: float | None = None,
    rows_loaded: int | None = None,
    dashboard_url: str | None = None,
) -> None:
    """Bridge a webhook notification (fire-and-forget).  Never raises."""
    try:
        if _event_loop is None or sender is None or not sender.is_configured:
            return
        coro = sender.send(
            event_type=event_type,
            schedule_name=schedule_name,
            sources=sources,
            error=error,
            duration_seconds=duration_seconds,
            stale_hours=stale_hours,
            rows_loaded=rows_loaded,
            dashboard_url=dashboard_url,
        )
        asyncio.run_coroutine_threadsafe(coro, _event_loop)
    except Exception:  # noqa: BLE001
        logger.warning("notify_bridge_error", exc_info=True)


def _ts() -> str:
    """UTC ISO timestamp for broadcast messages."""
    return datetime.now(tz=timezone.utc).isoformat()


def _build_dashboard_url(project_root: Path) -> str | None:
    """Build the dashboard URL, preferring domain over localhost.  Never raises."""
    try:
        from dango.config import ConfigLoader

        loader = ConfigLoader(project_root)
        cloud = loader.load_cloud_config()
        if cloud is not None and cloud.domain:
            return f"https://{cloud.domain}"
        config = loader.load_config()
        return f"http://localhost:{config.platform.port}"
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


def _resolve_sources(project_root: Path, names: list[str]) -> list[DataSource]:
    """Resolve source names to ``DataSource`` objects.  Unknown names are skipped."""
    from dango.config.helpers import load_config

    config = load_config(project_root)
    resolved: list[DataSource] = []
    for name in names:
        source = config.sources.get_source(name)
        if source is None:
            logger.warning("unknown_source_name", source=name)
        else:
            resolved.append(source)
    return resolved


# ---------------------------------------------------------------------------
# Execution history (structlog)
# ---------------------------------------------------------------------------


def _log_execution_event(
    *,
    schedule_name: str,
    job_type: str,
    status: str,
    duration_seconds: float,
    error: str | None = None,
    sources: list[str] | None = None,
) -> None:
    """Log a structured execution record (complements SQLite history)."""
    logger.info(
        "job_execution_recorded",
        schedule_name=schedule_name,
        job_type=job_type,
        status=status,
        duration_seconds=round(duration_seconds, 2),
        error=error,
        sources=sources or [],
    )


# ---------------------------------------------------------------------------
# SQLite history helpers
# ---------------------------------------------------------------------------


def _try_record_start(
    project_root: Path,
    schedule_name: str,
    source_names: list[str] | None = None,
) -> int | None:
    """Attempt to record job start in SQLite history. Returns record_id or None."""
    if _scheduler_service is None:
        return None
    try:
        from dango.platform.scheduling.history import get_scheduler_db_path, record_start

        db_path = get_scheduler_db_path(project_root)
        record_id = record_start(db_path, schedule_name, sources=source_names)
        _scheduler_service.register_execution(f"schedule:{schedule_name}", record_id)
        return record_id
    except Exception:  # noqa: BLE001
        logger.debug("record_start_failed", exc_info=True)
        return None


def _try_finish_record(
    project_root: Path,
    schedule_name: str,
    record_id: int | None,
    finish_func_name: str,
    **kwargs: Any,
) -> None:
    """Attempt to finalize a history record (completion/failure/timeout/cancel)."""
    if record_id is None or _scheduler_service is None:
        return
    try:
        from dango.platform.scheduling import history as hist_mod

        db_path = hist_mod.get_scheduler_db_path(project_root)
        func = getattr(hist_mod, finish_func_name)
        func(db_path, record_id, **kwargs)
        _scheduler_service._running_records.pop(f"schedule:{schedule_name}", None)
    except Exception:  # noqa: BLE001
        logger.debug("record_finish_failed", func=finish_func_name, exc_info=True)


# ---------------------------------------------------------------------------
# Data freshness check
# ---------------------------------------------------------------------------


def _check_freshness(
    project_root: Path,
    schedule_name: str,
    source_names: list[str],
    sender: Any,
) -> None:
    """Check data freshness; notify if any source is stale."""
    try:
        from dango.platform.notifications.webhook import EventType, load_notification_config
        from dango.utils.sync_history import load_sync_history

        notif_config = load_notification_config(project_root)
        if notif_config is None:
            return
        threshold_hours = notif_config.stale_threshold_hours
        now = datetime.now(tz=timezone.utc)

        for source_name in source_names:
            history = load_sync_history(project_root, source_name, limit=1)
            if not history:
                continue
            last = history[0]
            ts_str = last.get("completed_at") or last.get("timestamp", "")
            if not ts_str:
                continue
            # Python 3.10 compat: strip timezone suffix before parsing
            clean = ts_str.replace("+00:00", "").replace("Z", "").replace("+0000", "")
            try:
                completed_at = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            age_hours = (now - completed_at).total_seconds() / 3600
            if age_hours > threshold_hours:
                logger.warning(
                    "source_data_stale",
                    source=source_name,
                    age_hours=round(age_hours, 1),
                    threshold_hours=threshold_hours,
                )
                _broadcast(
                    {
                        "event": "sync_stale",
                        "source": source_name,
                        "schedule": schedule_name,
                        "age_hours": round(age_hours, 1),
                        "threshold_hours": threshold_hours,
                        "timestamp": now.isoformat(),
                    }
                )
                _notify(
                    sender,
                    event_type=EventType.SYNC_STALE,
                    schedule_name=schedule_name,
                    sources=[source_name],
                    stale_hours=round(age_hours, 1),
                )
    except Exception:  # noqa: BLE001
        logger.debug("freshness_check_error", exc_info=True)


# ---------------------------------------------------------------------------
# Pending dbt sources — shared state for coalescing
# ---------------------------------------------------------------------------

_PENDING_DBT_FILE = ".dango/state/pending_dbt_sources.json"


def _add_pending_dbt_source(project_root: Path, source_name: str) -> None:
    """Atomically add a source to the pending dbt list."""
    path = project_root / _PENDING_DBT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            data = json.load(f)
        except (json.JSONDecodeError, ValueError):
            data = []
        if source_name not in data:
            data.append(source_name)
        f.seek(0)
        f.truncate()
        json.dump(data, f)


def _consume_pending_dbt_sources(project_root: Path) -> list[str]:
    """Atomically read and clear the pending dbt sources list."""
    path = project_root / _PENDING_DBT_FILE
    if not path.exists():
        return []
    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = json.load(f)
        except (json.JSONDecodeError, ValueError):
            data = []
        f.seek(0)
        f.truncate()
        json.dump([], f)
    return list(data)


def _get_coalesce_seconds(project_root: Path) -> int:
    """Read dbt_coalesce_seconds from config."""
    try:
        from dango.config import ConfigLoader

        config = ConfigLoader(project_root).load_config()
        return config.platform.dbt_coalesce_seconds
    except Exception:
        return 10


def _run_coalesced_dbt(project_root: Path) -> bool:
    """Run dbt for all pending sources, plus dbt docs and Metabase refresh.

    Returns True if dbt succeeded (or no pending sources), False on failure.
    """
    coalesce_seconds = _get_coalesce_seconds(project_root)
    if coalesce_seconds > 0:
        time.sleep(coalesce_seconds)

    pending = _consume_pending_dbt_sources(project_root)
    if not pending:
        return True

    from dango.transformation import generate_dbt_docs, run_dbt_models

    select_criteria = " ".join(f"source:{s}+" for s in pending)
    logger.info("coalesced_dbt_run", sources=pending, select=select_criteria)
    dbt_success, _dbt_output = run_dbt_models(project_root, select=select_criteria)

    if dbt_success:
        # Generate docs and refresh Metabase (mirrors run_sync post-dbt steps)
        generate_dbt_docs(project_root)
        try:
            from dango.visualization.metabase import (
                refresh_metabase_connection,
                sync_metabase_schema,
            )

            if refresh_metabase_connection(project_root):
                sync_metabase_schema(project_root)
        except Exception:
            logger.debug("metabase_refresh_after_coalesced_dbt_failed", exc_info=True)
    else:
        logger.error("coalesced_dbt_run_failed", sources=pending, select=select_criteria)

    return dbt_success


# ---------------------------------------------------------------------------
# Main job functions
# ---------------------------------------------------------------------------


def run_scheduled_sync(schedule_name: str, sources: list[str], **kwargs: Any) -> None:
    """Run a scheduled data sync for the given sources.

    Called by APScheduler from a thread-pool executor.  Launches each source
    sync in a subprocess (via ``sync_process.launch_sync_subprocess``),
    keeping the DbtLock out of the web server process.

    Sources are synced individually with cancellation checks between each source.
    dbt is coalesced after all sources complete.

    Args:
        schedule_name: Human-readable schedule identifier.
        sources: List of source names to sync.
        **kwargs: Must include ``project_root`` (str).
    """
    project_root = Path(kwargs.get("project_root", "."))
    full_refresh = bool(kwargs.get("full_refresh", False))
    skip_dbt = bool(kwargs.get("skip_dbt", False))
    t0 = time.monotonic()

    from dango.exceptions import JobCancelledError, JobTimeoutError
    from dango.platform.notifications.webhook import (
        EventType,
        WebhookSender,
        load_notification_config,
    )
    from dango.platform.sync_process import (
        cleanup_sync_status,
        launch_sync_subprocess,
        poll_sync_status_blocking,
    )

    sender = WebhookSender(load_notification_config(project_root))
    source_names = list(sources)

    record_id: int | None = None

    try:
        resolved = _resolve_sources(project_root, source_names)
        if not resolved:
            elapsed = time.monotonic() - t0
            logger.warning("no_sources_resolved", schedule=schedule_name, names=source_names)
            _log_execution_event(
                schedule_name=schedule_name,
                job_type="sync",
                status="no_sources",
                duration_seconds=elapsed,
                sources=source_names,
            )
            return

        record_id = _try_record_start(project_root, schedule_name, source_names)

        _broadcast(
            {
                "event": "sync_started",
                "schedule": schedule_name,
                "sources": source_names,
                "timestamp": _ts(),
            }
        )

        job_id = f"schedule:{schedule_name}"
        total_rows = 0

        # Sync each source in a subprocess with skip_dbt=True
        # (dbt coalesced after all sources)
        for src in resolved:
            if _scheduler_service is not None and _scheduler_service.is_cancelled(job_id):
                raise JobCancelledError(f"Sync cancelled between sources for {schedule_name}")

            process, sync_id = launch_sync_subprocess(
                project_root,
                sources=[src.name],
                full_refresh=full_refresh,
                skip_dbt=True,
                source_label="scheduler",
                max_lock_wait=300,
            )

            success, _result = poll_sync_status_blocking(
                project_root,
                process,
                source_name=src.name,
                sync_id=sync_id,
                broadcast_fn=_broadcast,
            )

            if not success:
                error_msg = (
                    _result.get("error", "Unknown error") if _result else "Subprocess failed"
                )
                raise RuntimeError(f"Sync failed for {src.name}: {error_msg}")

            # Accumulate rows_loaded from subprocess result
            src_rows = 0
            if _result and isinstance(_result, dict):
                src_rows = _result.get("rows_loaded", 0)
                total_rows += src_rows

            if not skip_dbt:
                _add_pending_dbt_source(project_root, src.name)
            cleanup_sync_status(project_root, sync_id=sync_id)

        # Run coalesced dbt (waits for coalesce window, merges pending sources)
        transform_error: str | None = None
        if not skip_dbt:
            dbt_ok = _run_coalesced_dbt(project_root)

            if not dbt_ok:
                transform_error = "Coalesced dbt run failed after sync"
                _broadcast(
                    {
                        "event": "dbt_run_all_failed",
                        "schedule": schedule_name,
                        "sources": source_names,
                        "message": transform_error,
                        "timestamp": _ts(),
                    }
                )
                # Record transform_error in sync history (mirrors dlt_runner behavior)
                try:
                    from dango.utils.sync_history import update_last_sync_entry

                    for src_name in source_names:
                        update_last_sync_entry(
                            project_root, src_name, {"transform_error": transform_error}
                        )
                except Exception:  # noqa: BLE001
                    logger.debug("transform_error_history_update_failed", exc_info=True)

        elapsed = time.monotonic() - t0

        dashboard_url = _build_dashboard_url(project_root)

        _try_finish_record(project_root, schedule_name, record_id, "record_completion")

        _log_execution_event(
            schedule_name=schedule_name,
            job_type="sync",
            status="completed",
            duration_seconds=elapsed,
            error=transform_error,
            sources=source_names,
        )
        _broadcast(
            {
                "event": "sync_completed",
                "schedule": schedule_name,
                "sources": source_names,
                "duration_seconds": round(elapsed, 2),
                "transform_error": transform_error,
                "timestamp": _ts(),
            }
        )
        _notify(
            sender,
            event_type=EventType.SYNC_COMPLETED,
            schedule_name=schedule_name,
            sources=source_names,
            duration_seconds=round(elapsed, 2),
            rows_loaded=total_rows,
            dashboard_url=dashboard_url,
        )
        _check_freshness(project_root, schedule_name, source_names, sender)

    except JobTimeoutError:
        elapsed = time.monotonic() - t0
        _try_finish_record(project_root, schedule_name, record_id, "record_timeout")
        _log_execution_event(
            schedule_name=schedule_name,
            job_type="sync",
            status="timeout",
            duration_seconds=elapsed,
            error="Job timed out",
            sources=source_names,
        )
        _broadcast(
            {
                "event": "sync_failed",
                "schedule": schedule_name,
                "sources": source_names,
                "error": "Job timed out",
                "timestamp": _ts(),
            }
        )
        _notify(
            sender,
            event_type=EventType.SYNC_FAILED,
            schedule_name=schedule_name,
            sources=source_names,
            error="Job timed out",
            duration_seconds=elapsed,
        )

    except JobCancelledError:
        elapsed = time.monotonic() - t0
        _try_finish_record(project_root, schedule_name, record_id, "record_cancellation")
        _log_execution_event(
            schedule_name=schedule_name,
            job_type="sync",
            status="cancelled",
            duration_seconds=elapsed,
            error="Job cancelled",
            sources=source_names,
        )
        _broadcast(
            {
                "event": "sync_failed",
                "schedule": schedule_name,
                "sources": source_names,
                "error": "Job cancelled",
                "timestamp": _ts(),
            }
        )
        _notify(
            sender,
            event_type=EventType.SYNC_FAILED,
            schedule_name=schedule_name,
            sources=source_names,
            error="Job cancelled",
            duration_seconds=elapsed,
        )

    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        error_msg = str(exc)
        logger.error(
            "scheduled_sync_failed",
            schedule=schedule_name,
            sources=source_names,
            error=error_msg,
            exc_info=True,
        )
        _try_finish_record(
            project_root, schedule_name, record_id, "record_failure", error=error_msg
        )
        _log_execution_event(
            schedule_name=schedule_name,
            job_type="sync",
            status="failed",
            duration_seconds=elapsed,
            error=error_msg,
            sources=source_names,
        )
        _broadcast(
            {
                "event": "sync_failed",
                "schedule": schedule_name,
                "sources": source_names,
                "error": error_msg,
                "timestamp": _ts(),
            }
        )
        _notify(
            sender,
            event_type=EventType.SYNC_FAILED,
            schedule_name=schedule_name,
            sources=source_names,
            error=error_msg,
            duration_seconds=elapsed,
        )


def run_scheduled_dbt(
    schedule_name: str,
    dbt_command: str | None = None,
    **kwargs: Any,
) -> None:
    """Run a scheduled dbt transformation.

    Called by APScheduler from a thread-pool executor.  Wraps
    ``run_dbt_models()`` with DbtLock serialization, WebSocket
    broadcasting, webhook notifications, and execution history recording.

    Args:
        schedule_name: Human-readable schedule identifier.
        dbt_command: dbt selection criteria.  ``None`` runs all models.
        **kwargs: Must include ``project_root`` (str).
    """
    project_root = Path(kwargs.get("project_root", "."))
    t0 = time.monotonic()

    from dango.exceptions import DbtLockError, JobCancelledError, JobTimeoutError
    from dango.platform.notifications.webhook import (
        EventType,
        WebhookSender,
        load_notification_config,
    )
    from dango.utils.dbt_lock import DbtLock

    sender = WebhookSender(load_notification_config(project_root))
    lock = DbtLock(project_root, source="scheduler", operation=f"dbt:{schedule_name}")

    record_id: int | None = None

    try:
        try:
            lock.acquire()
        except DbtLockError:
            elapsed = time.monotonic() - t0
            logger.warning(
                "scheduler_lock_contention", schedule=schedule_name, dbt_command=dbt_command
            )
            _broadcast(
                {
                    "event": "job_queued",
                    "schedule": schedule_name,
                    "message": "Lock contention",
                    "timestamp": _ts(),
                }
            )
            _broadcast(
                {
                    "event": "dbt_failed",
                    "schedule": schedule_name,
                    "error": "Could not acquire lock",
                    "timestamp": _ts(),
                }
            )
            _notify(
                sender,
                event_type=EventType.SYNC_FAILED,
                schedule_name=schedule_name,
                error="Could not acquire lock",
                duration_seconds=elapsed,
            )
            _log_execution_event(
                schedule_name=schedule_name,
                job_type="dbt",
                status="lock_failed",
                duration_seconds=elapsed,
                error="Lock contention",
            )
            return

        record_id = _try_record_start(project_root, schedule_name, source_names=None)

        _broadcast(
            {
                "event": "dbt_started",
                "schedule": schedule_name,
                "dbt_command": dbt_command,
                "timestamp": _ts(),
            }
        )

        from dango.transformation import run_dbt_models

        success, output = run_dbt_models(project_root, select=dbt_command)
        elapsed = time.monotonic() - t0

        dbt_dashboard_url = _build_dashboard_url(project_root)

        if success:
            _try_finish_record(project_root, schedule_name, record_id, "record_completion")
            _log_execution_event(
                schedule_name=schedule_name,
                job_type="dbt",
                status="completed",
                duration_seconds=elapsed,
            )
            _broadcast(
                {
                    "event": "dbt_completed",
                    "schedule": schedule_name,
                    "duration_seconds": round(elapsed, 2),
                    "timestamp": _ts(),
                }
            )
            _notify(
                sender,
                event_type=EventType.SYNC_COMPLETED,
                schedule_name=schedule_name,
                duration_seconds=round(elapsed, 2),
                dashboard_url=dbt_dashboard_url,
            )
        else:
            _try_finish_record(
                project_root, schedule_name, record_id, "record_failure", error=output
            )
            _log_execution_event(
                schedule_name=schedule_name,
                job_type="dbt",
                status="failed",
                duration_seconds=elapsed,
                error=output,
            )
            _broadcast(
                {
                    "event": "dbt_failed",
                    "schedule": schedule_name,
                    "error": output,
                    "timestamp": _ts(),
                }
            )
            _notify(
                sender,
                event_type=EventType.SYNC_FAILED,
                schedule_name=schedule_name,
                error=output,
                duration_seconds=elapsed,
            )

    except JobTimeoutError:
        elapsed = time.monotonic() - t0
        _try_finish_record(project_root, schedule_name, record_id, "record_timeout")
        _log_execution_event(
            schedule_name=schedule_name,
            job_type="dbt",
            status="timeout",
            duration_seconds=elapsed,
            error="Job timed out",
        )
        _broadcast(
            {
                "event": "dbt_failed",
                "schedule": schedule_name,
                "error": "Job timed out",
                "timestamp": _ts(),
            }
        )
        _notify(
            sender,
            event_type=EventType.SYNC_FAILED,
            schedule_name=schedule_name,
            error="Job timed out",
            duration_seconds=elapsed,
        )

    except JobCancelledError:
        elapsed = time.monotonic() - t0
        _try_finish_record(project_root, schedule_name, record_id, "record_cancellation")
        _log_execution_event(
            schedule_name=schedule_name,
            job_type="dbt",
            status="cancelled",
            duration_seconds=elapsed,
            error="Job cancelled",
        )
        _notify(
            sender,
            event_type=EventType.SYNC_FAILED,
            schedule_name=schedule_name,
            error="Job cancelled",
            duration_seconds=elapsed,
        )

    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        error_msg = str(exc)
        logger.error(
            "scheduled_dbt_failed",
            schedule=schedule_name,
            dbt_command=dbt_command,
            error=error_msg,
            exc_info=True,
        )
        _try_finish_record(
            project_root, schedule_name, record_id, "record_failure", error=error_msg
        )
        _log_execution_event(
            schedule_name=schedule_name,
            job_type="dbt",
            status="failed",
            duration_seconds=elapsed,
            error=error_msg,
        )
        _broadcast(
            {
                "event": "dbt_failed",
                "schedule": schedule_name,
                "error": error_msg,
                "timestamp": _ts(),
            }
        )
        _notify(
            sender,
            event_type=EventType.SYNC_FAILED,
            schedule_name=schedule_name,
            error=error_msg,
            duration_seconds=elapsed,
        )
    finally:
        lock.release()
