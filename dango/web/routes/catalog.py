"""dango/web/routes/catalog.py

Data catalog API endpoints for column schema introspection, profiling,
lineage, impact analysis, and unified model browsing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.catalog.lineage import (  # noqa: F401 — _get_impact_tree for integration tests
    _build_impact_response,
    _build_lineage_dag,
    _get_impact_tree,
)
from dango.catalog.manifest import (
    _find_model_in_manifest,
    _model_profiling_key,
    _search_manifest,
)
from dango.catalog.models import (
    _build_catalog_models,
    _build_model_detail,
    _get_run_results,
    _get_source_summary_stats,
)
from dango.catalog.profiling import (
    _get_cached_row_counts,  # noqa: F401 — needed for test patching
    _get_cached_stats,
    _get_profiled_at,
)
from dango.catalog.schema import (
    _get_column_schema,
    _get_model_column_schema,
    _get_raw_tables_from_duckdb,
    _get_row_count,
    _source_schema_exists,
    _table_exists,
)
from dango.logging import get_logger
from dango.utils.post_sync import _run_profiling, profile_table
from dango.validation import validate_identifier, validate_source_name
from dango.web.helpers import get_dbt_manifest, get_project_root  # Imported for test patching

logger = get_logger(__name__)

router = APIRouter(tags=["catalog"])


# ---------------------------------------------------------------------------
# Shared validation
# ---------------------------------------------------------------------------


async def _validate_and_resolve(
    source: str,
    table: str,
    project_root: Path,
) -> Path:
    """Validate inputs and return the DuckDB path, raising 404 as needed.

    Args:
        source: Raw source path parameter (validated + lowercased).
        table: Raw table path parameter (validated + lowercased).
        project_root: Dango project root.

    Returns:
        Path to the DuckDB warehouse file.

    Raises:
        HTTPException: 404 if DuckDB missing, schema missing, or table missing.
    """
    db_path = project_root / "data" / "warehouse.duckdb"

    if not db_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Data warehouse not found. Run a sync first.",
        )

    if not await asyncio.to_thread(_source_schema_exists, db_path, source):
        raise HTTPException(
            status_code=404,
            detail=f"Source '{source}' has no data. Run a sync first.",
        )

    if not await asyncio.to_thread(_table_exists, db_path, source, table):
        raise HTTPException(
            status_code=404,
            detail=f"Table '{table}' not found in source '{source}'.",
        )

    return db_path


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/catalog/{source}/{table}/columns")
async def get_table_columns(
    source: str,
    table: str,
    user: User = Depends(require_permission("governance.view")),  # noqa: ARG001
) -> dict[str, Any]:
    """Return column schema and cached profiling stats for a table.

    Args:
        source: Source name (URL path parameter).
        table: Table name (URL path parameter).
        user: Authenticated user with ``governance.view`` permission.

    Returns:
        Column schema with optional cached profiling statistics.
    """
    source = validate_source_name(source)
    table = validate_identifier(table)
    project_root = get_project_root()
    db_path = await _validate_and_resolve(source, table, project_root)

    columns, cached_stats, row_count, profiled_at, manifest = await asyncio.gather(
        asyncio.to_thread(_get_column_schema, db_path, source, table),
        asyncio.to_thread(_get_cached_stats, project_root, source, table),
        asyncio.to_thread(_get_row_count, db_path, source, table),
        asyncio.to_thread(_get_profiled_at, project_root, source, table),
        asyncio.to_thread(get_dbt_manifest),
    )

    # Merge column descriptions from manifest if available
    col_descriptions: dict[str, str] = {}
    if manifest:
        for src in manifest.get("sources", {}).values():
            if src.get("name") == table:
                src_schema = src.get("schema", "")
                if src_schema == f"raw_{source}" or src_schema == source:
                    for cname, cinfo in src.get("columns", {}).items():
                        desc = cinfo.get("description", "")
                        if desc:
                            col_descriptions[cname] = desc
                    break

    for col in columns:
        col["stats"] = cached_stats.get(col["name"])
        col["description"] = col_descriptions.get(col["name"])

    return {
        "source": source,
        "table": table,
        "row_count": row_count,
        "profiled_at": profiled_at,
        "columns": columns,
    }


@router.post("/api/catalog/{source}/{table}/profile")
async def refresh_table_profile(
    source: str,
    table: str,
    user: User = Depends(require_permission("governance.view")),  # noqa: ARG001
) -> dict[str, Any]:
    """Compute fresh profiling stats for a table and return them.

    Args:
        source: Source name (URL path parameter).
        table: Table name (URL path parameter).
        user: Authenticated user with ``governance.view`` permission.

    Returns:
        Column schema with freshly computed profiling statistics.
    """
    source = validate_source_name(source)
    table = validate_identifier(table)
    project_root = get_project_root()
    db_path = await _validate_and_resolve(source, table, project_root)

    try:
        fresh_stats = await asyncio.to_thread(
            profile_table,
            project_root,
            source,
            table,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Profiling failed for {source}.{table}: {type(exc).__name__}",
        ) from exc

    columns, row_count, profiled_at = await asyncio.gather(
        asyncio.to_thread(_get_column_schema, db_path, source, table),
        asyncio.to_thread(_get_row_count, db_path, source, table),
        asyncio.to_thread(_get_profiled_at, project_root, source, table),
    )

    for col in columns:
        col["stats"] = fresh_stats.get(col["name"])

    return {
        "source": source,
        "table": table,
        "row_count": row_count,
        "profiled_at": profiled_at,
        "columns": columns,
    }


@router.post("/api/catalog/profile-all")
async def profile_all_models(
    user: User = Depends(require_permission("dbt.run")),  # noqa: ARG001
) -> dict[str, Any]:
    """Re-profile all raw tables, staging models, and dbt models.

    Populates row counts and column stats in the profiling cache without
    requiring a sync — covers projects set up via data import and syncs where
    every source failed.

    Args:
        user: Authenticated user with ``dbt.run`` permission.

    Returns:
        Dict with the profiling status and the discovered source names.
    """
    project_root = get_project_root()
    db_path = project_root / "data" / "warehouse.duckdb"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="No warehouse database found. Sync data first.")

    raw_tables = await asyncio.to_thread(_get_raw_tables_from_duckdb, db_path)
    source_names = sorted({rt["source_name"] for rt in raw_tables})
    if not source_names:
        raise HTTPException(status_code=404, detail="No tables found to profile.")

    try:
        await asyncio.to_thread(_run_profiling, project_root, source_names)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Profiling failed: {type(exc).__name__}"
        ) from exc

    log_auth_event(
        AuditEvent.CATALOG_PROFILE_TRIGGERED,
        user_id=user.id,
        email=user.email,
        details={"sources": source_names},
    )

    return {"status": "ok", "sources": source_names}


# ---------------------------------------------------------------------------
# Catalog model endpoints
# ---------------------------------------------------------------------------


@router.get("/api/catalog/models")
async def list_catalog_models(
    user: User = Depends(require_permission("governance.view")),  # noqa: ARG001
) -> dict[str, Any]:
    """List all models and sources from the dbt manifest.

    Returns models categorized by type (staging/intermediate/marts) and
    sources.  Includes raw tables not yet modeled in dbt and an overview
    summary with source freshness.

    Args:
        user: Authenticated user with ``governance.view`` permission.

    Returns:
        Dict with ``models``, ``sources``, and ``overview`` keys.
    """
    from dango.web.helpers import get_source_freshness, load_sources_config

    manifest, run_results = await asyncio.gather(
        asyncio.to_thread(get_dbt_manifest),
        asyncio.to_thread(_get_run_results),
    )

    if manifest is None:
        result: dict[str, Any] = {"models": [], "sources": []}
    else:
        result = await asyncio.to_thread(_build_catalog_models, manifest, run_results)

    project_root = get_project_root()
    db_path = project_root / "data" / "warehouse.duckdb"
    source_stats: dict[str, dict[str, int]] = {}
    if db_path.exists():
        source_stats = await asyncio.to_thread(_get_source_summary_stats, db_path)

    # BUG-128: Overview summary with source freshness
    sources_config = await asyncio.to_thread(load_sources_config)
    freshness_list = await asyncio.gather(
        *[asyncio.to_thread(get_source_freshness, src.get("name", "")) for src in sources_config],
        return_exceptions=True,
    )
    freshness_items: list[dict[str, Any]] = []
    for i, f in enumerate(freshness_list):
        if isinstance(f, BaseException):
            logger.warning("freshness_check_failed", source=sources_config[i].get("name", ""))
            freshness_items.append(
                {
                    "source": sources_config[i].get("name", ""),
                    "status": None,
                    "hours_since_sync": None,
                }
            )
        else:
            freshness_items.append(
                {
                    "source": sources_config[i].get("name", ""),
                    "status": f.get("status"),
                    "hours_since_sync": f.get("hours_since_sync"),
                }
            )

    # BUG-155: Per-source breakdown with table count, row count, freshness
    freshness_by_source = {item["source"]: item["status"] for item in freshness_items}
    sources_detail: list[dict[str, Any]] = []
    for src_cfg in sources_config:
        src_name = src_cfg.get("name", "")
        stats = source_stats.get(src_name, {"table_count": 0, "estimated_row_total": 0})
        sources_detail.append(
            {
                "name": src_name,
                "table_count": stats["table_count"],
                "estimated_row_total": stats["estimated_row_total"],
                "freshness_status": freshness_by_source.get(src_name),
            }
        )

    result["overview"] = {
        "source_count": len(sources_config),
        "table_count": len(result["sources"]),
        "model_count": len(result["models"]),
        "freshness": freshness_items,
        "sources_detail": sources_detail,
    }

    return result


@router.get("/api/catalog/models/{model_name}")
async def get_catalog_model(
    model_name: str,
    user: User = Depends(require_permission("governance.view")),  # noqa: ARG001
) -> dict[str, Any]:
    """Return detailed information for a single model or source.

    Includes column schema (merged with manifest descriptions), SQL code,
    test results, tags, and dependency information.

    Args:
        model_name: Model or source name (URL path parameter).
        user: Authenticated user with ``governance.view`` permission.

    Returns:
        Full model/source detail response.
    """
    model_name = validate_identifier(model_name)
    project_root = get_project_root()

    manifest, run_results = await asyncio.gather(
        asyncio.to_thread(get_dbt_manifest),
        asyncio.to_thread(_get_run_results),
    )

    if manifest is None:
        raise HTTPException(
            status_code=404,
            detail="No dbt manifest found. Run dbt first.",
        )

    target_uid, target_node, kind = _find_model_in_manifest(manifest, model_name)
    if target_uid is None or target_node is None:
        # BUG-132: Fallback — check if this table exists in DuckDB raw schemas
        db_path = project_root / "data" / "warehouse.duckdb"
        if db_path.exists():
            raw_tables = await asyncio.to_thread(_get_raw_tables_from_duckdb, db_path)
            match = next((rt for rt in raw_tables if rt["table"] == model_name), None)
            if match:
                raw_schema = match["schema"]
                raw_source_name = match["source_name"]
                raw_cols = await asyncio.to_thread(
                    _get_model_column_schema, db_path, raw_schema, model_name
                )
                raw_profiled_at = await asyncio.to_thread(
                    _get_profiled_at, project_root, raw_source_name, model_name
                )
                # Build minimal response
                columns = []
                for col in raw_cols:
                    columns.append({**col, "description": "", "tests": []})
                # Inject cached stats
                cached_stats = await asyncio.to_thread(
                    _get_cached_stats, project_root, raw_source_name, model_name
                )
                if cached_stats:
                    for col in columns:
                        col["stats"] = cached_stats.get(col["name"])
                return {
                    "name": model_name,
                    "type": "source",
                    "schema": raw_schema,
                    "source_name": raw_source_name,
                    "description": "",
                    "materialization": None,
                    "columns": columns,
                    "profiled_at": raw_profiled_at,
                    "row_count": None,
                    "tests": [],
                    "tags": [],
                    "depends_on": [],
                    "depended_on_by": [],
                    "raw_code": None,
                    "compiled_code": None,
                }
        raise HTTPException(
            status_code=404,
            detail=f"Model or source '{model_name}' not found in manifest.",
        )

    # Determine schema + table for DuckDB column lookup
    db_path = project_root / "data" / "warehouse.duckdb"
    schema = target_node.get("schema", "")
    table = target_node.get("name", "")

    db_columns: list[dict[str, Any]] = []
    profiled_at: str | None = None

    if db_path.exists() and schema and table:
        db_columns = await asyncio.to_thread(_get_model_column_schema, db_path, schema, table)

    # Get profiled_at from the profiling cache, keyed identically to the write path
    cache_key = _model_profiling_key(target_node, kind)
    if cache_key:
        cache_source, cache_table = cache_key
        profiled_at = await asyncio.to_thread(
            _get_profiled_at, project_root, cache_source, cache_table
        )

    result = _build_model_detail(
        manifest, run_results, target_uid, target_node, kind, db_columns, profiled_at
    )

    # Get row count if DuckDB is available and table exists
    if db_path.exists() and schema and table and db_columns:

        def _count_rows() -> int | None:
            try:
                conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
                try:
                    row = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()
                    return row[0] if row else 0
                finally:
                    conn.close()
            except Exception:
                return None

        row_count = await asyncio.to_thread(_count_rows)
        if row_count is not None:
            result["row_count"] = row_count

    # Inject cached profiling stats for any table with profiling data
    if cache_key and result.get("columns"):
        cache_source, cache_table = cache_key
        cached_stats = await asyncio.to_thread(
            _get_cached_stats, project_root, cache_source, cache_table
        )
        if cached_stats:
            for col in result["columns"]:
                col["stats"] = cached_stats.get(col["name"])

    return result


@router.get("/api/catalog/search")
async def search_catalog(
    q: str = Query(..., min_length=2, max_length=100),
    user: User = Depends(require_permission("governance.view")),  # noqa: ARG001
) -> dict[str, Any]:
    """Search across model names, descriptions, and column names.

    Args:
        q: Search query (2–100 characters).
        user: Authenticated user with ``governance.view`` permission.

    Returns:
        Dict with ``query`` and ``results`` list.
    """
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Query must be at least 2 characters.")

    manifest = await asyncio.to_thread(get_dbt_manifest)
    if manifest is None:
        return {"query": query, "results": []}

    results = await asyncio.to_thread(_search_manifest, manifest, query)
    return {"query": query, "results": results}


# ---------------------------------------------------------------------------
# Lineage endpoints
# ---------------------------------------------------------------------------


@router.get("/api/catalog/lineage")
async def get_lineage(
    user: User = Depends(require_permission("governance.view")),  # noqa: ARG001
) -> dict[str, Any]:
    """Return the full lineage DAG from the dbt manifest.

    Args:
        user: Authenticated user with ``governance.view`` permission.

    Returns:
        Dict with ``nodes`` and ``edges`` lists.
    """
    dag = await asyncio.to_thread(_build_lineage_dag)
    if dag is None:
        raise HTTPException(
            status_code=404,
            detail="No dbt manifest found. Run dbt first to generate lineage data.",
        )
    return dag


@router.get("/api/catalog/impact/{model_name}")
async def get_impact(
    model_name: str,
    user: User = Depends(require_permission("governance.view")),  # noqa: ARG001
) -> dict[str, Any]:
    """Return the downstream impact tree for a model.

    Args:
        model_name: Model name (URL path parameter).
        user: Authenticated user with ``governance.view`` permission.

    Returns:
        Impact tree with direct dependents and total downstream count.
    """
    model_name = validate_identifier(model_name)
    manifest = await asyncio.to_thread(get_dbt_manifest)
    if manifest is None:
        raise HTTPException(
            status_code=404,
            detail="No dbt manifest found. Run dbt first to generate lineage data.",
        )

    result = await asyncio.to_thread(_build_impact_response, manifest, model_name)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{model_name}' not found in dbt manifest.",
        )
    return result
