"""dango/web/routes/ai.py

Agent/AI interface endpoints for metadata discovery and tool descriptions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import duckdb
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import has_permission, require_permission
from dango.logging import get_logger
from dango.validation import validate_identifier, validate_source_name
from dango.web.helpers import (
    get_dbt_models,
    get_duckdb_path,
    get_project_root,
    get_source_freshness,
    get_source_tables_info,
    load_sources_config,
)

logger = get_logger(__name__)

router = APIRouter(tags=["ai"])


# ---------------------------------------------------------------------------
# Response models (scoped to AI endpoints)
# ---------------------------------------------------------------------------


class ColumnSummary(BaseModel):
    """Column metadata for agent consumption."""

    name: str
    type: str
    nullable: bool


class TableSummary(BaseModel):
    """Table metadata including column schema."""

    name: str
    source: str
    row_count: int | None
    columns: list[ColumnSummary]


class QualitySignals(BaseModel):
    """Data quality signals from governance subsystem."""

    drift_event_count: int = 0
    pii_column_count: int = 0


class SourceSummary(BaseModel):
    """Source metadata with tables and quality signals."""

    name: str
    type: str
    enabled: bool
    status: str
    last_sync_time: str | None
    row_count: int | None
    freshness: dict[str, Any] | None
    tables: list[TableSummary]
    quality: QualitySignals


class DbtModelSummary(BaseModel):
    """dbt model metadata for agent consumption."""

    name: str
    unique_id: str
    path: str
    materialization: str | None
    schema_name: str | None
    description: str
    depends_on: list[str]
    tags: list[str]
    row_count: int | None
    last_run: str | None
    status: str | None


class CatalogSummaryResponse(BaseModel):
    """Full catalog summary combining sources and dbt models."""

    description: str
    generated_at: str
    sources: list[SourceSummary]
    dbt_models: list[DbtModelSummary]
    totals: dict[str, int]


class ToolParameter(BaseModel):
    """Parameter description for a tool endpoint."""

    name: str
    type: str
    required: bool
    description: str
    enum: list[str] | None = None


class ToolDescription(BaseModel):
    """Describes an available API tool for agent use."""

    name: str
    description: str
    endpoint: str
    method: str
    parameters: list[ToolParameter]
    permissions_required: list[str]
    is_read_only: bool
    is_safe_to_retry: bool


class ToolsResponse(BaseModel):
    """List of available tools with user role context."""

    description: str
    tools: list[ToolDescription]
    user_role: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_column_schema(db_path: str, source: str, table: str) -> list[dict[str, Any]]:
    """Query DuckDB information_schema for column metadata.

    Validates source/table names, uses read-only connection.
    Returns empty list on any error.
    """
    try:
        schema = f"raw_{validate_source_name(source)}"
        safe_table = validate_identifier(table)
        conn = duckdb.connect(db_path, config={"access_mode": "read_only"})
        try:
            rows = conn.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                f"WHERE table_schema = '{schema}' AND table_name = '{safe_table}' "
                "ORDER BY ordinal_position"
            ).fetchall()
            return [{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


async def _build_source_summary(
    source_cfg: dict[str, Any],
    db_path_str: str | None,
    has_governance: bool,
    project_root: Any,
) -> SourceSummary:
    """Build a SourceSummary for a single source.

    Fetches freshness, tables, and optionally quality signals concurrently
    via asyncio.to_thread.
    """
    name = source_cfg.get("name", "")
    source_type = source_cfg.get("type", "unknown")
    enabled = source_cfg.get("enabled", True)

    freshness_data, tables_info = await asyncio.gather(
        asyncio.to_thread(get_source_freshness, name),
        asyncio.to_thread(get_source_tables_info, name),
    )

    status = freshness_data.get("status", "unknown") if freshness_data else "unknown"
    last_sync_time = freshness_data.get("last_sync_time") if freshness_data else None

    # Build table summaries with column schema
    table_summaries: list[TableSummary] = []
    total_rows: int | None = None
    if tables_info is not None:
        total_rows = tables_info.get("total_rows")
        for tbl in tables_info.get("tables", []):
            cols: list[dict[str, Any]] = []
            if db_path_str is not None:
                cols = await asyncio.to_thread(
                    _get_column_schema,
                    db_path_str,
                    name,
                    tbl["name"],
                )
            table_summaries.append(
                TableSummary(
                    name=tbl["name"],
                    source=name,
                    row_count=tbl.get("row_count"),
                    columns=[ColumnSummary(**c) for c in cols],
                )
            )

    # Freshness dict for response
    freshness: dict[str, Any] | None = None
    if freshness_data:
        freshness = {
            "status": freshness_data.get("status"),
            "hours_since_sync": freshness_data.get("hours_since_sync"),
            "last_sync_status": freshness_data.get("last_sync_status"),
        }

    # Quality signals
    quality = QualitySignals()
    if has_governance:
        try:
            from dango.governance.schema_drift import get_drift_history

            drift_events = await asyncio.to_thread(get_drift_history, project_root, source=name)
            quality.drift_event_count = len(drift_events)
        except Exception:
            logger.debug("Failed to fetch drift history for %s", name, exc_info=True)

        try:
            from dango.governance.pii_detector import get_pii_findings

            pii_findings = await asyncio.to_thread(get_pii_findings, project_root, source=name)
            quality.pii_column_count = len(pii_findings)
        except Exception:
            logger.debug("Failed to fetch PII findings for %s", name, exc_info=True)

    return SourceSummary(
        name=name,
        type=source_type,
        enabled=enabled,
        status=status,
        last_sync_time=last_sync_time,
        row_count=total_rows,
        freshness=freshness,
        tables=table_summaries,
        quality=quality,
    )


# ---------------------------------------------------------------------------
# GET /api/catalog/summary
# ---------------------------------------------------------------------------


@router.get("/api/catalog/summary")
async def get_catalog_summary(
    user: User = Depends(require_permission("source.view")),
) -> CatalogSummaryResponse:
    """Return a machine-readable summary of all data sources and dbt models.

    Designed for LLM/agent consumption. Includes table schemas, freshness,
    row counts, and quality signals (drift + PII) when the user has
    governance.view permission.
    """
    log_auth_event(
        AuditEvent.AI_CATALOG_VIEWED,
        user_id=user.id,
        email=user.email,
    )

    project_root = get_project_root()
    has_governance = has_permission(user, "governance.view")

    # Fetch sources config and dbt models concurrently
    sources_config, dbt_models_raw = await asyncio.gather(
        asyncio.to_thread(load_sources_config),
        asyncio.to_thread(get_dbt_models),
    )

    # Build source summaries concurrently
    db_path = get_duckdb_path()
    db_path_str: str | None = str(db_path) if db_path.exists() else None

    source_summaries = await asyncio.gather(
        *[
            _build_source_summary(src, db_path_str, has_governance, project_root)
            for src in sources_config
        ]
    )

    # Build dbt model summaries
    dbt_models = [
        DbtModelSummary(
            name=m.get("name", ""),
            unique_id=m.get("unique_id", ""),
            path=m.get("path", ""),
            materialization=m.get("materialization"),
            schema_name=m.get("schema"),
            description=m.get("description", ""),
            depends_on=m.get("depends_on", []),
            tags=m.get("tags", []),
            row_count=m.get("row_count"),
            last_run=m.get("last_run"),
            status=m.get("status"),
        )
        for m in dbt_models_raw
    ]

    total_tables = sum(len(s.tables) for s in source_summaries)

    return CatalogSummaryResponse(
        description=(
            "Dango data catalog summary. Contains all configured data sources "
            "with table schemas, freshness, and quality signals, plus dbt "
            "transformation models."
        ),
        generated_at=datetime.now(timezone.utc).isoformat(),
        sources=list(source_summaries),
        dbt_models=dbt_models,
        totals={
            "source_count": len(source_summaries),
            "table_count": total_tables,
            "model_count": len(dbt_models),
        },
    )


# ---------------------------------------------------------------------------
# GET /api/tools
# ---------------------------------------------------------------------------

# fmt: off
_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_sources",
        "description": "List all configured data sources with sync status and row counts.",
        "endpoint": "/api/sources",
        "method": "GET",
        "parameters": [],
        "permissions_required": ["source.view"],
        "is_read_only": True,
        "is_safe_to_retry": True,
    },
    {
        "name": "get_source_details",
        "description": "Get detailed information for a specific data source including tables, freshness, and sync history.",
        "endpoint": "/api/sources/{source_name}/details",
        "method": "GET",
        "parameters": [
            {"name": "source_name", "type": "string", "required": True, "description": "Name of the data source.", "enum": None},
        ],
        "permissions_required": ["source.view"],
        "is_read_only": True,
        "is_safe_to_retry": True,
    },
    {
        "name": "get_catalog_summary",
        "description": "Get a machine-readable summary of all data sources, tables, columns, and dbt models with quality signals.",
        "endpoint": "/api/catalog/summary",
        "method": "GET",
        "parameters": [],
        "permissions_required": ["source.view"],
        "is_read_only": True,
        "is_safe_to_retry": True,
    },
    {
        "name": "get_table_schema",
        "description": "Get column schema and profiling statistics for a specific table.",
        "endpoint": "/api/catalog/{source}/{table}/columns",
        "method": "GET",
        "parameters": [
            {"name": "source", "type": "string", "required": True, "description": "Source name.", "enum": None},
            {"name": "table", "type": "string", "required": True, "description": "Table name.", "enum": None},
        ],
        "permissions_required": ["governance.view"],
        "is_read_only": True,
        "is_safe_to_retry": True,
    },
    {
        "name": "check_freshness",
        "description": "Check data freshness for a specific source (last sync time, hours since sync).",
        "endpoint": "/api/sources/{source_name}/details",
        "method": "GET",
        "parameters": [
            {"name": "source_name", "type": "string", "required": True, "description": "Name of the data source.", "enum": None},
        ],
        "permissions_required": ["source.view"],
        "is_read_only": True,
        "is_safe_to_retry": True,
    },
    {
        "name": "get_schema_drift",
        "description": "Get schema drift events showing column additions, removals, and type changes.",
        "endpoint": "/api/governance/schema-drift",
        "method": "GET",
        "parameters": [
            {"name": "source", "type": "string", "required": False, "description": "Filter by source name.", "enum": None},
            {"name": "table", "type": "string", "required": False, "description": "Filter by table name.", "enum": None},
            {"name": "limit", "type": "integer", "required": False, "description": "Max events to return (default 100).", "enum": None},
        ],
        "permissions_required": ["governance.view"],
        "is_read_only": True,
        "is_safe_to_retry": True,
    },
    {
        "name": "get_pii_findings",
        "description": "Get PII detection findings showing columns containing personally identifiable information.",
        "endpoint": "/api/governance/pii",
        "method": "GET",
        "parameters": [
            {"name": "source", "type": "string", "required": False, "description": "Filter by source name.", "enum": None},
            {"name": "table", "type": "string", "required": False, "description": "Filter by table name.", "enum": None},
            {"name": "limit", "type": "integer", "required": False, "description": "Max findings to return (default 100).", "enum": None},
        ],
        "permissions_required": ["governance.view"],
        "is_read_only": True,
        "is_safe_to_retry": True,
    },
    {
        "name": "list_dbt_models",
        "description": "List all dbt transformation models with their status, dependencies, and row counts.",
        "endpoint": "/api/dbt/models",
        "method": "GET",
        "parameters": [],
        "permissions_required": ["dbt.view"],
        "is_read_only": True,
        "is_safe_to_retry": True,
    },
    {
        "name": "sync_source",
        "description": "Trigger a data sync for a specific source. This ingests fresh data from the external service.",
        "endpoint": "/api/sources/{source_name}/sync",
        "method": "POST",
        "parameters": [
            {"name": "source_name", "type": "string", "required": True, "description": "Name of the source to sync.", "enum": None},
        ],
        "permissions_required": ["source.sync"],
        "is_read_only": False,
        "is_safe_to_retry": False,
    },
    {
        "name": "run_dbt_model",
        "description": "Run a specific dbt transformation model to refresh derived tables.",
        "endpoint": "/api/dbt/models/{model_name}/run",
        "method": "POST",
        "parameters": [
            {"name": "model_name", "type": "string", "required": True, "description": "Name of the dbt model to run.", "enum": None},
        ],
        "permissions_required": ["dbt.run"],
        "is_read_only": False,
        "is_safe_to_retry": False,
    },
]
# fmt: on


@router.get("/api/tools")
async def get_tools(
    user: User = Depends(require_permission("source.view")),
) -> ToolsResponse:
    """Return descriptions of all available API tools for agent use.

    All tools are visible to all authenticated users. Each tool includes
    permissions_required metadata so agents know which actions they can
    perform. RBAC enforcement happens at the target endpoint.
    """
    log_auth_event(
        AuditEvent.AI_CATALOG_VIEWED,
        user_id=user.id,
        email=user.email,
        details={"endpoint": "tools"},
    )

    tools = [
        ToolDescription(
            name=t["name"],
            description=t["description"],
            endpoint=t["endpoint"],
            method=t["method"],
            parameters=[ToolParameter(**p) for p in t["parameters"]],
            permissions_required=t["permissions_required"],
            is_read_only=t["is_read_only"],
            is_safe_to_retry=t["is_safe_to_retry"],
        )
        for t in _TOOL_DEFINITIONS
    ]

    return ToolsResponse(
        description=(
            "Available Dango API tools. Each tool maps to an API endpoint. "
            "Check permissions_required before calling mutating endpoints."
        ),
        tools=tools,
        user_role=user.role.value,
    )
