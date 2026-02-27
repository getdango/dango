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

        future = asyncio.run_coroutine_threadsafe(ws_manager.broadcast(message), _event_loop)
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
        )
        asyncio.run_coroutine_threadsafe(coro, _event_loop)
    except Exception:  # noqa: BLE001
        logger.warning("notify_bridge_error", exc_info=True)


def _ts() -> str:
    """UTC ISO timestamp for broadcast messages."""
    return datetime.now(tz=timezone.utc).isoformat()


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
# Main job functions
# ---------------------------------------------------------------------------


def run_scheduled_sync(schedule_name: str, sources: list[str], **kwargs: Any) -> None:
    """Run a scheduled data sync for the given sources.

    Called by APScheduler from a thread-pool executor.  Wraps ``run_sync()``
    with DbtLock serialization, WebSocket broadcasting, webhook notifications,
    and execution history recording.

    Sources are synced individually with cancellation checks between each source.

    Args:
        schedule_name: Human-readable schedule identifier.
        sources: List of source names to sync.
        **kwargs: Must include ``project_root`` (str).
    """
    project_root = Path(kwargs.get("project_root", "."))
    full_refresh = bool(kwargs.get("full_refresh", False))
    t0 = time.monotonic()

    from dango.exceptions import DbtLockError, JobCancelledError, JobTimeoutError
    from dango.platform.notifications.webhook import (
        EventType,
        WebhookSender,
        load_notification_config,
    )
    from dango.utils.dbt_lock import DbtLock

    sender = WebhookSender(load_notification_config(project_root))
    source_names = list(sources)
    lock = DbtLock(project_root, source="scheduler", operation=f"sync:{schedule_name}")

    record_id: int | None = None

    try:
        try:
            lock.acquire()
        except DbtLockError:
            elapsed = time.monotonic() - t0
            logger.warning(
                "scheduler_lock_contention", schedule=schedule_name, sources=source_names
            )
            _broadcast(
                {
                    "event": "job_queued",
                    "schedule": schedule_name,
                    "sources": source_names,
                    "message": "Lock contention",
                    "timestamp": _ts(),
                }
            )
            _broadcast(
                {
                    "event": "sync_failed",
                    "schedule": schedule_name,
                    "sources": source_names,
                    "error": "Could not acquire lock",
                    "timestamp": _ts(),
                }
            )
            _notify(
                sender,
                event_type=EventType.SYNC_FAILED,
                schedule_name=schedule_name,
                sources=source_names,
                error="Could not acquire lock",
                duration_seconds=elapsed,
            )
            _log_execution_event(
                schedule_name=schedule_name,
                job_type="sync",
                status="lock_failed",
                duration_seconds=elapsed,
                error="Lock contention",
                sources=source_names,
            )
            return

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

        from dango.ingestion.dlt_runner import run_sync
        from dango.utils.sync_history import save_sync_history_entry

        job_id = f"schedule:{schedule_name}"
        for src in resolved:
            if _scheduler_service is not None and _scheduler_service.is_cancelled(job_id):
                raise JobCancelledError(f"Sync cancelled between sources for {schedule_name}")
            src_t0 = time.monotonic()
            run_sync(project_root, [src], full_refresh=full_refresh)
            save_sync_history_entry(
                project_root,
                src.name,
                {
                    "trigger": "scheduler",
                    "schedule": schedule_name,
                    "status": "completed",
                    "completed_at": _ts(),
                    "duration_seconds": round(time.monotonic() - src_t0, 2),
                },
            )

        elapsed = time.monotonic() - t0

        _try_finish_record(project_root, schedule_name, record_id, "record_completion")

        _log_execution_event(
            schedule_name=schedule_name,
            job_type="sync",
            status="completed",
            duration_seconds=elapsed,
            sources=source_names,
        )
        _broadcast(
            {
                "event": "sync_completed",
                "schedule": schedule_name,
                "sources": source_names,
                "duration_seconds": round(elapsed, 2),
                "timestamp": _ts(),
            }
        )
        _notify(
            sender,
            event_type=EventType.SYNC_COMPLETED,
            schedule_name=schedule_name,
            sources=source_names,
            duration_seconds=round(elapsed, 2),
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
    finally:
        lock.release()


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
