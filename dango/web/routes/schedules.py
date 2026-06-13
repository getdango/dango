"""dango/web/routes/schedules.py

Read-only API endpoints for schedule viewing, manual triggering, execution
history, reload from YAML, cancellation, unscheduled-source discovery, and
notification config/test.  Schedule and webhook definitions are managed
exclusively via the CLI (``dango schedule add``, ``dango schedule webhook add``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

import dango
from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.config.schedules import (
    ScheduleConfig,
    SchedulesConfig,
    get_schedule_job_id,
    load_schedules_config,
    reload_schedules,
)
from dango.logging import get_logger
from dango.platform.notifications.webhook import (
    EventType,
    NotificationConfig,
    WebhookSender,
    load_notification_config,
)
from dango.platform.scheduling.history import (
    VALID_STATUSES,
    get_last_run,
    get_recent_history,
    get_schedule_history,
    get_scheduler_db_path,
)
from dango.web.helpers import append_log_entry, get_project_root, load_sources_config
from dango.web.models import TriggerRequest
from dango.web.routes.ui import _render_template

if TYPE_CHECKING:
    from dango.platform.scheduling.scheduler import SchedulerService

router = APIRouter(tags=["schedules"])
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_scheduler(request: Request) -> SchedulerService | None:
    """Get scheduler from app state (may be ``None`` if startup failed)."""
    return getattr(request.app.state, "scheduler", None)


def _find_closest_entry(
    history: list[dict[str, Any]],
    target: datetime,
    tolerance_seconds: int = 300,
) -> dict[str, Any] | None:
    """Find sync history entry closest to target timestamp within tolerance."""
    best: dict[str, Any] | None = None
    best_diff = float("inf")
    for entry in history:
        ts = entry.get("started_at") or entry.get("timestamp")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        diff = abs((t - target).total_seconds())
        if diff < best_diff and diff <= tolerance_seconds:
            best_diff, best = diff, entry
    return best


def _get_source_errors(
    sources: list[str],
    schedule_started_at: str | None,
) -> dict[str, dict[str, Any]] | None:
    """Get per-source error details for a failed schedule run."""
    if not schedule_started_at or not sources:
        return None
    from dango.web.helpers import load_sync_history

    try:
        target = datetime.fromisoformat(schedule_started_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

    errors: dict[str, dict[str, Any]] = {}
    for source_name in sources:
        history = load_sync_history(source_name, limit=5)
        match = _find_closest_entry(history, target)
        if match:
            errors[source_name] = {
                "status": match.get("status", "unknown"),
                "error": match.get("error"),
            }
    return errors or None


def _find_schedule(config: SchedulesConfig, name: str) -> ScheduleConfig | None:
    """Find a schedule by name in the config."""
    for sched in config.schedules:
        if sched.name == name:
            return sched
    return None


def _audit(
    event: AuditEvent,
    user: User,
    request: Request,
    project_root: Path,
    **extra: Any,
) -> None:
    """Log an audit event with standard fields."""
    log_auth_event(
        event,
        user_id=user.id,
        email=user.email,
        ip=request.client.host if request.client else None,
        details=extra,
        log_dir=project_root / ".dango" / "logs",
    )


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------


@router.get("/schedules")
async def schedules_page(
    request: Request,
    user: User = Depends(require_permission("scheduler.view")),
) -> HTMLResponse:
    """Render the schedule management UI page."""
    return _render_template(
        request,
        "schedules.html",
        {
            "version": dango.__version__,
            "current_page": "schedules",
            "subtitle": "Schedules",
        },
    )


# ---------------------------------------------------------------------------
# Literal routes (must be registered BEFORE parameterized {name} routes)
# ---------------------------------------------------------------------------


@router.get("/api/notifications/config")
async def get_notification_config(
    user: User = Depends(require_permission("scheduler.view")),
) -> JSONResponse:
    """Get the current notification configuration."""
    project_root = get_project_root()
    config = load_notification_config(project_root) or NotificationConfig()

    return JSONResponse(
        content={
            "webhooks": [wh.model_dump() for wh in config.webhooks],
            "on_failure": config.on_failure,
            "on_success": config.on_success,
            "on_stale": config.on_stale,
            "stale_threshold_hours": config.stale_threshold_hours,
        }
    )


@router.post("/api/notifications/test")
async def test_notification(
    request: Request,
    user: User = Depends(require_permission("scheduler.manage")),
) -> JSONResponse:
    """Send a test notification to all configured webhooks."""
    project_root = get_project_root()
    config = load_notification_config(project_root)

    if config is None or len(config.webhooks) == 0:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-S008",
                "message": "No webhooks configured. Run `dango schedule webhook add` to configure.",
            },
        )

    sender = WebhookSender(config)
    # Override filtering so the test fires regardless of on_success/on_failure config
    await sender.send(
        event_type=EventType.SYNC_COMPLETED,
        schedule_name="test",
        sources=["test"],
        schedule_notify_on={"on_success": True, "on_failure": True, "on_stale": True},
    )

    _audit(
        AuditEvent.SCHEDULE_TRIGGERED,
        user,
        request,
        project_root,
        action="test_notification",
    )

    return JSONResponse(content={"status": "sent"})


@router.get("/api/schedules/history/recent")
async def recent_executions(
    user: User = Depends(require_permission("scheduler.view")),
    limit: int = Query(default=20, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Get the most recent execution records across all schedules."""
    project_root = get_project_root()
    db_path = get_scheduler_db_path(project_root)

    if not db_path.exists():
        return []

    return get_recent_history(db_path, limit=limit)


@router.post("/api/internal/schedules/reload")
async def internal_reload_schedules(request: Request) -> JSONResponse:
    """CLI-triggered schedule reload -- localhost only, no auth required."""
    # Reject requests that came through a reverse proxy (Caddy/nginx always
    # adds X-Forwarded-For). Without this, cloud requests through Caddy would
    # appear as localhost since Caddy proxies to 127.0.0.1:8800.
    if request.headers.get("x-forwarded-for"):
        return JSONResponse(status_code=403, content={"error": "Localhost only"})
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1"):
        return JSONResponse(status_code=403, content={"error": "Localhost only"})

    scheduler = _get_scheduler(request)
    if scheduler is None:
        return JSONResponse(
            status_code=503,
            content={"message": "Scheduler not available"},
        )

    project_root = get_project_root()
    config = load_schedules_config(project_root)
    result = reload_schedules(scheduler, config.schedules, project_root)

    logger.info(
        "schedules_reloaded_by_cli",
        added=result.added,
        removed=result.removed,
        updated=result.updated,
    )

    return JSONResponse(content=result.model_dump(mode="json"))


@router.get("/api/schedules")
async def list_schedules(
    request: Request,
    user: User = Depends(require_permission("scheduler.view")),
) -> JSONResponse:
    """List all configured schedules with runtime status."""
    project_root = get_project_root()
    config = load_schedules_config(project_root)
    scheduler = _get_scheduler(request)

    # Build next_run_time lookup from APScheduler jobs
    next_runs: dict[str, str] = {}
    if scheduler is not None:
        for job in scheduler.get_jobs():
            if job.next_run_time is not None:
                next_runs[job.id] = job.next_run_time.isoformat()

    # Build last_run lookup from history DB
    db_path = get_scheduler_db_path(project_root)
    last_runs: dict[str, dict[str, Any]] = {}
    if db_path.exists():
        for sched in config.schedules:
            last = get_last_run(db_path, sched.name)
            if last is not None:
                last_runs[sched.name] = last

    result: list[dict[str, Any]] = []
    for sched in config.schedules:
        job_id = get_schedule_job_id(sched.name)
        last_run = last_runs.get(sched.name)
        entry: dict[str, Any] = {
            "name": sched.name,
            "type": sched.type.value,
            "cron": sched.cron,
            "sources": list(sched.sources),
            "enabled": sched.enabled,
            "next_run_time": next_runs.get(job_id),
            "last_run": last_run,
        }
        # Add per-source error details for failed runs
        entry["source_errors"] = None
        if last_run and last_run.get("status") == "failed":
            entry["source_errors"] = await asyncio.to_thread(
                _get_source_errors, list(sched.sources), last_run.get("started_at")
            )
        result.append(entry)

    return JSONResponse(content=result)


@router.post("/api/schedules/reload")
async def reload_schedules_endpoint(
    request: Request,
    user: User = Depends(require_permission("scheduler.manage")),
) -> JSONResponse:
    """Reload schedules from YAML config."""
    project_root = get_project_root()
    scheduler = _get_scheduler(request)

    if scheduler is None:
        return JSONResponse(
            status_code=503,
            content={
                "error_code": "DANGO-S006",
                "message": "Scheduler is not available.",
            },
        )

    config = load_schedules_config(project_root)
    result = reload_schedules(scheduler, config.schedules, project_root)

    _audit(
        AuditEvent.SCHEDULES_RELOADED,
        user,
        request,
        project_root,
        added=result.added,
        removed=result.removed,
        updated=result.updated,
    )

    return JSONResponse(
        content=result.model_dump(mode="json"),
    )


@router.post("/api/schedules/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    request: Request,
    user: User = Depends(require_permission("scheduler.manage")),
) -> JSONResponse:
    """Cancel a currently running job."""
    project_root = get_project_root()
    scheduler = _get_scheduler(request)

    if scheduler is None:
        return JSONResponse(
            status_code=503,
            content={
                "error_code": "DANGO-S006",
                "message": "Scheduler is not available.",
            },
        )

    cancelled = scheduler.cancel_job(job_id)
    if not cancelled:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-S007",
                "message": f"Job {job_id!r} is not currently running.",
            },
        )

    _audit(
        AuditEvent.JOB_CANCELLED,
        user,
        request,
        project_root,
        job_id=job_id,
    )

    return JSONResponse(content={"status": "cancelled", "job_id": job_id})


@router.get("/api/sources/unscheduled")
async def unscheduled_sources(
    user: User = Depends(require_permission("scheduler.view")),
) -> JSONResponse:
    """List sources that are not referenced by any schedule."""
    project_root = get_project_root()
    sources = load_sources_config()
    source_names = {s["name"] for s in sources if "name" in s}

    config = load_schedules_config(project_root)
    scheduled_sources: set[str] = set()
    for sched in config.schedules:
        scheduled_sources.update(sched.sources)

    unscheduled = sorted(source_names - scheduled_sources)
    return JSONResponse(content={"sources": unscheduled})


# ---------------------------------------------------------------------------
# Parameterized {name} routes (MUST come after literal routes)
# ---------------------------------------------------------------------------


@router.get("/api/schedules/{name}")
async def get_schedule(
    name: str,
    request: Request,
    user: User = Depends(require_permission("scheduler.view")),
) -> JSONResponse:
    """Get details for a single schedule."""
    project_root = get_project_root()
    config = load_schedules_config(project_root)
    sched = _find_schedule(config, name)

    if sched is None:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-S003",
                "message": f"Schedule {name!r} not found.",
            },
        )

    # Get next_run_time from APScheduler
    scheduler = _get_scheduler(request)
    next_run_time: str | None = None
    if scheduler is not None:
        job_id = get_schedule_job_id(name)
        for job in scheduler.get_jobs():
            if job.id == job_id and job.next_run_time is not None:
                next_run_time = job.next_run_time.isoformat()
                break

    # Get last 10 history records
    db_path = get_scheduler_db_path(project_root)
    history: list[dict[str, Any]] = []
    if db_path.exists():
        history, _ = get_schedule_history(db_path, name, limit=10)

    result = sched.model_dump(exclude_none=True, mode="json")
    result["next_run_time"] = next_run_time
    result["recent_history"] = history

    return JSONResponse(content=result)


@router.post("/api/schedules/{name}/trigger")
async def trigger_schedule(
    name: str,
    request: Request,
    body: TriggerRequest | None = None,
    user: User = Depends(require_permission("source.sync")),
) -> JSONResponse:
    """Manually trigger a schedule for immediate execution."""
    project_root = get_project_root()
    config = load_schedules_config(project_root)
    sched = _find_schedule(config, name)

    if sched is None:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-S003",
                "message": f"Schedule {name!r} not found.",
            },
        )

    scheduler = _get_scheduler(request)
    if scheduler is None:
        return JSONResponse(
            status_code=503,
            content={
                "error_code": "DANGO-S006",
                "message": "Scheduler is not available.",
            },
        )

    # Add a one-off job using the same function + kwargs as the scheduled version
    from apscheduler.triggers.date import DateTrigger

    from dango.config.schedules import ScheduleType
    from dango.platform.scheduling.jobs import (
        run_scheduled_dbt,
        run_scheduled_sync,
    )

    func: Any
    if sched.type in (ScheduleType.SYNC, ScheduleType.SYNC_ONLY):
        func = run_scheduled_sync
        func_kwargs: dict[str, Any] = {
            "schedule_name": sched.name,
            "sources": list(sched.sources),
            "project_root": str(project_root),
            "skip_dbt": sched.type == ScheduleType.SYNC_ONLY,
        }
    else:
        func = run_scheduled_dbt
        func_kwargs = {
            "schedule_name": sched.name,
            "dbt_command": sched.dbt_command,
            "project_root": str(project_root),
        }

    job = scheduler.add_job(
        func,
        DateTrigger(),
        kwargs=func_kwargs,
        id=f"manual:{name}:{datetime.now(tz=timezone.utc).isoformat()}",
        name=f"manual-{name}",
    )

    _audit(
        AuditEvent.SCHEDULE_TRIGGERED,
        user,
        request,
        project_root,
        schedule_name=name,
    )

    append_log_entry(
        {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level": "info",
            "source": f"schedule:{name}",
            "message": "Schedule manually triggered",
        }
    )

    return JSONResponse(
        content={"status": "triggered", "name": name, "job_id": job.id},
    )


@router.get("/api/schedules/{name}/history")
async def schedule_history(
    name: str,
    user: User = Depends(require_permission("scheduler.view")),
    status: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """Get paginated execution history for a specific schedule."""
    if status is not None and status not in VALID_STATUSES:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-S001",
                "message": (
                    f"Invalid status filter. Must be one of: {', '.join(sorted(VALID_STATUSES))}"
                ),
            },
        )

    for label, value in [("since", since), ("until", until)]:
        if value is not None:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error_code": "DANGO-S002",
                        "message": (f"Invalid '{label}' parameter. Must be an ISO 8601 timestamp."),
                    },
                )

    project_root = get_project_root()
    db_path = get_scheduler_db_path(project_root)

    if not db_path.exists():
        return JSONResponse(
            content={
                "schedule_name": name,
                "items": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
            }
        )

    items, total = get_schedule_history(
        db_path,
        name,
        status=status,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )

    return JSONResponse(
        content={
            "schedule_name": name,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )
