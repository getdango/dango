"""dango/web/routes/schedules.py

API endpoints for schedule execution history. Provides paginated history
per schedule and a recent-executions endpoint across all schedules.

Extended by TASK-038 with schedule CRUD endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.logging import get_logger
from dango.platform.scheduling.history import (
    VALID_STATUSES,
    get_recent_history,
    get_schedule_history,
    get_scheduler_db_path,
)
from dango.web.helpers import get_project_root

router = APIRouter(tags=["schedules"])
logger = get_logger(__name__)


@router.get("/api/schedules/history/recent")
async def recent_executions(
    user: User = Depends(require_permission("scheduler.view")),
    limit: int = Query(default=20, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Get the most recent execution records across all schedules.

    Args:
        user: Authenticated user with ``scheduler.view`` permission.
        limit: Maximum number of records to return.

    Returns:
        List of execution history records.
    """
    project_root = get_project_root()
    db_path = get_scheduler_db_path(project_root)

    if not db_path.exists():
        return []

    return get_recent_history(db_path, limit=limit)


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
    """Get paginated execution history for a specific schedule.

    Args:
        name: Schedule name.
        user: Authenticated user with ``scheduler.view`` permission.
        status: Optional status filter (running/success/failed/cancelled/timeout).
        since: Optional ISO 8601 lower bound for started_at.
        until: Optional ISO 8601 upper bound for started_at.
        limit: Maximum records per page.
        offset: Records to skip.

    Returns:
        JSON with schedule_name, items, total, limit, and offset.
    """
    if status is not None and status not in VALID_STATUSES:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-S001",
                "message": f"Invalid status filter. Must be one of: {', '.join(sorted(VALID_STATUSES))}",
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
                        "message": f"Invalid '{label}' parameter. Must be an ISO 8601 timestamp.",
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
