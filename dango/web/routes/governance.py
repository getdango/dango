"""dango/web/routes/governance.py

Data governance API endpoints.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.governance.models import (
    AcceptDriftResponse,
    DriftEvent,
    DriftResponse,
    PiiFinding,
    PiiOverride,
    PiiOverrideRequest,
    PiiOverridesResponse,
    PiiResponse,
    SourceAttention,
)
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


@router.get("/api/governance/pii/overrides")
async def list_pii_overrides(
    source: str | None = Query(None, description="Filter by source name"),
    user: User = Depends(require_permission("governance.view")),
) -> PiiOverridesResponse:
    """List all PII overrides."""
    project_root = get_project_root()

    validated_source: str | None = None
    if source is not None:
        validated_source = validate_source_name(source)

    from dango.governance.pii_overrides import get_pii_overrides

    overrides = await asyncio.to_thread(
        get_pii_overrides,
        project_root,
        source=validated_source,
    )

    log_auth_event(
        AuditEvent.GOVERNANCE_PII_VIEWED,
        user_id=user.id,
        email=user.email,
        details={"source": validated_source, "view": "overrides"},
    )

    pii_overrides = [PiiOverride(**o) for o in overrides]
    return PiiOverridesResponse(overrides=pii_overrides, count=len(pii_overrides))


@router.put("/api/governance/pii/override")
async def set_pii_override_endpoint(
    body: PiiOverrideRequest,
    user: User = Depends(require_permission("governance.manage")),
) -> dict[str, str]:
    """Set a PII override for a column (create or update)."""
    project_root = get_project_root()

    validated_source = validate_source_name(body.source)
    validated_table = validate_identifier(body.table_name)
    validated_column = validate_identifier(body.column_name)

    from dango.governance.pii_overrides import set_pii_override

    await asyncio.to_thread(
        set_pii_override,
        project_root,
        validated_source,
        validated_table,
        validated_column,
        body.pii_status,
        user.email,
        body.reason,
    )

    log_auth_event(
        AuditEvent.GOVERNANCE_PII_OVERRIDE_SET,
        user_id=user.id,
        email=user.email,
        details={
            "source": validated_source,
            "table": validated_table,
            "column": validated_column,
            "pii_status": body.pii_status,
        },
    )

    return {"status": "ok"}


@router.delete("/api/governance/pii/override")
async def delete_pii_override_endpoint(
    source: str = Query(..., description="Source name"),
    table: str = Query(..., description="Table name"),
    column: str = Query(..., description="Column name"),
    user: User = Depends(require_permission("governance.manage")),
) -> dict[str, str]:
    """Delete a PII override for a column."""
    project_root = get_project_root()

    validated_source = validate_source_name(source)
    validated_table = validate_identifier(table)
    validated_column = validate_identifier(column)

    from dango.governance.pii_overrides import delete_pii_override

    deleted = await asyncio.to_thread(
        delete_pii_override,
        project_root,
        validated_source,
        validated_table,
        validated_column,
    )

    if deleted:
        log_auth_event(
            AuditEvent.GOVERNANCE_PII_OVERRIDE_DELETED,
            user_id=user.id,
            email=user.email,
            details={
                "source": validated_source,
                "table": validated_table,
                "column": validated_column,
            },
        )

    return {"status": "ok" if deleted else "not_found"}


@router.post("/api/governance/drift/{source}/accept")
async def accept_source_drift(
    source: str,
    user: User = Depends(require_permission("governance.manage")),
) -> AcceptDriftResponse:
    """Accept schema drift for a source and clear attention flag."""
    from fastapi import HTTPException

    source = validate_source_name(source)
    project_root = get_project_root()

    from dango.governance.schema_drift import accept_drift, get_sources_needing_attention

    # Verify the source actually has an attention flag
    attention = await asyncio.to_thread(get_sources_needing_attention, project_root)
    if not any(r["source"] == source for r in attention):
        raise HTTPException(status_code=404, detail=f"No pending drift for '{source}'")

    await asyncio.to_thread(accept_drift, project_root, source)

    log_auth_event(
        AuditEvent.GOVERNANCE_DRIFT_ACCEPTED,
        user_id=user.id,
        email=user.email,
        details={"source": source},
    )

    return AcceptDriftResponse(source=source, accepted=True, message="Schema accepted")


@router.get("/api/governance/attention")
async def get_attention_sources(
    user: User = Depends(require_permission("governance.view")),
) -> list[SourceAttention]:
    """Return sources with unresolved breaking drift."""
    project_root = get_project_root()

    from dango.governance.schema_drift import get_sources_needing_attention

    rows = await asyncio.to_thread(get_sources_needing_attention, project_root)
    return [SourceAttention(**r) for r in rows]
