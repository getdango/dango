"""dango/web/routes/governance.py

Data governance API endpoints.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.governance.models import DriftEvent, DriftResponse, PiiFinding, PiiResponse
from dango.logging import get_logger
from dango.validation import validate_identifier, validate_source_name
from dango.web.helpers import get_project_root

logger = get_logger(__name__)

router = APIRouter(tags=["governance"])


@router.get("/api/governance/schema-drift")
async def get_schema_drift(
    source: str | None = Query(None, description="Filter by source name"),
    table: str | None = Query(None, description="Filter by table name"),
    limit: int = Query(100, ge=1, le=1000, description="Max events"),
    user: User = Depends(require_permission("governance.view")),
) -> DriftResponse:
    """Query schema drift event history.

    Returns cached drift events from SQLite, newest first.
    """
    project_root = get_project_root()

    # Validate filters
    validated_source: str | None = None
    if source is not None:
        validated_source = validate_source_name(source)

    validated_table: str | None = None
    if table is not None:
        validated_table = validate_identifier(table)

    from dango.governance.schema_drift import get_drift_history

    events = await asyncio.to_thread(
        get_drift_history,
        project_root,
        source=validated_source,
        table_name=validated_table,
        limit=limit,
    )

    log_auth_event(
        AuditEvent.GOVERNANCE_DRIFT_VIEWED,
        user_id=user.id,
        email=user.email,
        details={"source": validated_source, "table": validated_table, "limit": limit},
    )

    drift_events = [DriftEvent(**ev) for ev in events]

    return DriftResponse(
        events=drift_events,
        count=len(drift_events),
        source=validated_source,
        table_name=validated_table,
    )


@router.get("/api/governance/pii")
async def get_pii(
    source: str | None = Query(None, description="Filter by source name"),
    table: str | None = Query(None, description="Filter by table name"),
    limit: int = Query(100, ge=1, le=1000, description="Max findings"),
    user: User = Depends(require_permission("governance.view")),
) -> PiiResponse:
    """Query cached PII findings.

    Returns PII findings from SQLite, newest first.
    """
    project_root = get_project_root()

    # Validate filters
    validated_source: str | None = None
    if source is not None:
        validated_source = validate_source_name(source)

    validated_table: str | None = None
    if table is not None:
        validated_table = validate_identifier(table)

    from dango.governance.pii_detector import get_pii_findings

    findings = await asyncio.to_thread(
        get_pii_findings,
        project_root,
        source=validated_source,
        table_name=validated_table,
        limit=limit,
    )

    log_auth_event(
        AuditEvent.GOVERNANCE_PII_VIEWED,
        user_id=user.id,
        email=user.email,
        details={"source": validated_source, "table": validated_table, "limit": limit},
    )

    pii_findings = [PiiFinding(**f) for f in findings]

    return PiiResponse(
        findings=pii_findings,
        count=len(pii_findings),
        source=validated_source,
        table_name=validated_table,
    )
