"""dango/web/routes/schedules.py

API endpoints for schedule management and execution history. Provides CRUD
for schedule definitions, manual triggering, reload from YAML, cancellation,
unscheduled-source discovery, and paginated execution history.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.config.schedules import (
    ScheduleConfig,
    SchedulesConfig,
    get_schedule_job_id,
    load_schedules_config,
    reload_schedules,
    save_schedules_config,
    validate_schedules,
)
from dango.logging import get_logger
from dango.platform.scheduling.history import (
    VALID_STATUSES,
    get_last_run,
    get_recent_history,
    get_schedule_history,
    get_scheduler_db_path,
)
from dango.web.helpers import get_project_root, load_sources_config
from dango.web.models import ScheduleCreateRequest, TriggerRequest

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
    project_root: Any,
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
# Literal routes (must be registered BEFORE parameterized {name} routes)
# ---------------------------------------------------------------------------


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
        entry: dict[str, Any] = {
            "name": sched.name,
            "type": sched.type.value,
            "cron": sched.cron,
            "sources": list(sched.sources),
            "enabled": sched.enabled,
            "next_run_time": next_runs.get(job_id),
            "last_run": last_runs.get(sched.name),
        }
        result.append(entry)

    return JSONResponse(content=result)


@router.post("/api/schedules")
async def create_schedule(
    request: Request,
    body: ScheduleCreateRequest,
    user: User = Depends(require_permission("scheduler.manage")),
) -> JSONResponse:
    """Create a new schedule definition."""
    project_root = get_project_root()
    config = load_schedules_config(project_root)

    # Check for duplicate name
    if _find_schedule(config, body.name) is not None:
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "DANGO-S005",
                "message": f"Schedule {body.name!r} already exists.",
            },
        )

    # Validate through ScheduleConfig (handles cron, name, type validation)
    try:
        new_sched = ScheduleConfig(**body.model_dump())
    except ValidationError as e:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-S004",
                "message": str(e),
            },
        )

    # Cross-validate against sources
    sources = load_sources_config()
    source_names = {s["name"] for s in sources if "name" in s}
    issues = validate_schedules([*config.schedules, new_sched], source_names)
    errors = [i for i in issues if not i.startswith("Schedule") or "warning" not in i.lower()]
    if errors:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-S004",
                "message": "; ".join(errors),
            },
        )

    # Persist
    updated_config = SchedulesConfig(schedules=[*config.schedules, new_sched])
    save_schedules_config(project_root, updated_config)

    # Reload scheduler
    scheduler = _get_scheduler(request)
    if scheduler is not None:
        reload_schedules(scheduler, updated_config.schedules, project_root)

    _audit(
        AuditEvent.SCHEDULE_CREATED,
        user,
        request,
        project_root,
        schedule_name=body.name,
    )

    return JSONResponse(
        status_code=201,
        content=new_sched.model_dump(exclude_none=True, mode="json"),
    )


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


@router.put("/api/schedules/{name}")
async def update_schedule(
    name: str,
    request: Request,
    body: ScheduleCreateRequest,
    user: User = Depends(require_permission("scheduler.manage")),
) -> JSONResponse:
    """Update an existing schedule definition."""
    project_root = get_project_root()
    config = load_schedules_config(project_root)
    existing = _find_schedule(config, name)

    if existing is None:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-S003",
                "message": f"Schedule {name!r} not found.",
            },
        )

    # Validate the updated schedule through ScheduleConfig
    try:
        updated_sched = ScheduleConfig(**body.model_dump())
    except ValidationError as e:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-S004",
                "message": str(e),
            },
        )

    # Replace in list
    new_schedules = [updated_sched if s.name == name else s for s in config.schedules]

    # Cross-validate
    sources = load_sources_config()
    source_names = {s["name"] for s in sources if "name" in s}
    issues = validate_schedules(new_schedules, source_names)
    errors = [i for i in issues if "Duplicate" in i or "unknown source" in i]
    if errors:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-S004",
                "message": "; ".join(errors),
            },
        )

    # Persist
    updated_config = SchedulesConfig(schedules=new_schedules)
    save_schedules_config(project_root, updated_config)

    # Reload scheduler
    scheduler = _get_scheduler(request)
    if scheduler is not None:
        reload_schedules(scheduler, updated_config.schedules, project_root)

    _audit(
        AuditEvent.SCHEDULE_UPDATED,
        user,
        request,
        project_root,
        schedule_name=name,
    )

    return JSONResponse(
        content=updated_sched.model_dump(exclude_none=True, mode="json"),
    )


@router.delete("/api/schedules/{name}")
async def delete_schedule(
    name: str,
    request: Request,
    user: User = Depends(require_permission("scheduler.manage")),
) -> JSONResponse:
    """Delete a schedule definition."""
    project_root = get_project_root()
    config = load_schedules_config(project_root)
    existing = _find_schedule(config, name)

    if existing is None:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-S003",
                "message": f"Schedule {name!r} not found.",
            },
        )

    # Remove from config
    new_schedules = [s for s in config.schedules if s.name != name]
    updated_config = SchedulesConfig(schedules=new_schedules)
    save_schedules_config(project_root, updated_config)

    # Remove from scheduler
    scheduler = _get_scheduler(request)
    if scheduler is not None:
        job_id = get_schedule_job_id(name)
        try:
            scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001
            logger.debug("schedule_job_remove_failed", name=name)

    _audit(
        AuditEvent.SCHEDULE_DELETED,
        user,
        request,
        project_root,
        schedule_name=name,
    )

    return JSONResponse(content={"status": "deleted", "name": name})


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
    if sched.type == ScheduleType.SYNC:
        func = run_scheduled_sync
        func_kwargs: dict[str, Any] = {
            "schedule_name": sched.name,
            "sources": list(sched.sources),
            "project_root": str(project_root),
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
        id=f"manual:{name}:{datetime.now().isoformat()}",
        name=f"manual-{name}",
    )

    _audit(
        AuditEvent.SCHEDULE_TRIGGERED,
        user,
        request,
        project_root,
        schedule_name=name,
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
