"""dango/web/routes/catalog.py

Data catalog API endpoints for column schema introspection, profiling,
lineage, and impact analysis.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException

from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.logging import get_logger
from dango.utils.dango_db import connect
from dango.utils.post_sync import profile_table
from dango.validation import validate_identifier, validate_source_name
from dango.web.helpers import get_dbt_manifest, get_project_root

logger = get_logger(__name__)

router = APIRouter(tags=["catalog"])


# ---------------------------------------------------------------------------
# Private blocking helpers (called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _get_column_schema(
    db_path: Path,
    source: str,
    table: str,
) -> list[dict[str, Any]]:
    """Query DuckDB for column metadata of a single table.

    Args:
        db_path: Path to the DuckDB warehouse file.
        source: Source name (schema is ``raw_{source}``).
        table: Table name.

    Returns:
        List of ``{"name": ..., "type": ..., "nullable": bool}``.
    """
    schema = f"raw_{source}"
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
            "ORDER BY ordinal_position"
        ).fetchall()
    finally:
        conn.close()

    return [{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in rows]


def _get_cached_stats(
    project_root: Path,
    source: str,
    table: str,
) -> dict[str, dict[str, str | None]]:
    """Read cached profiling stats from SQLite.

    Args:
        project_root: Dango project root.
        source: Source name.
        table: Table name.

    Returns:
        Mapping of ``{column_name: {stat_type: stat_value}}``.
    """
    result: dict[str, dict[str, str | None]] = {}
    with connect(project_root) as conn:
        rows = conn.execute(
            "SELECT column_name, stat_type, stat_value "
            "FROM profiling_stats WHERE source = ? AND table_name = ?",
            (source, table),
        ).fetchall()
    for row in rows:
        col_name = row[0]
        if col_name not in result:
            result[col_name] = {}
        result[col_name][row[1]] = row[2]
    return result


def _get_profiled_at(
    project_root: Path,
    source: str,
    table: str,
) -> str | None:
    """Return the most recent ``updated_at`` from profiling stats.

    Args:
        project_root: Dango project root.
        source: Source name.
        table: Table name.

    Returns:
        ISO timestamp string or ``None`` if no stats exist.
    """
    with connect(project_root) as conn:
        row = conn.execute(
            "SELECT MAX(updated_at) FROM profiling_stats WHERE source = ? AND table_name = ?",
            (source, table),
        ).fetchone()
    if row and row[0]:
        result: str = row[0]
        return result
    return None


def _get_row_count(db_path: Path, source: str, table: str) -> int:
    """Return the row count for a table.

    Args:
        db_path: Path to the DuckDB warehouse file.
        source: Source name.
        table: Table name.

    Returns:
        Number of rows.
    """
    schema = f"raw_{source}"
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        result = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()
    finally:
        conn.close()
    return result[0] if result else 0


def _source_schema_exists(db_path: Path, source: str) -> bool:
    """Check whether the ``raw_{source}`` schema exists in DuckDB.

    Args:
        db_path: Path to the DuckDB warehouse file.
        source: Source name.

    Returns:
        ``True`` if the schema exists.
    """
    schema = f"raw_{source}"
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        result = conn.execute(
            f"SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = '{schema}'"
        ).fetchone()
    finally:
        conn.close()
    return bool(result and result[0] > 0)


def _table_exists(db_path: Path, source: str, table: str) -> bool:
    """Check whether a user table exists in DuckDB (excluding ``_dlt_*``).

    Args:
        db_path: Path to the DuckDB warehouse file.
        source: Source name.
        table: Table name.

    Returns:
        ``True`` if the table exists.
    """
    schema = f"raw_{source}"
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        result = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
            "AND table_name NOT LIKE '_dlt_%'"
        ).fetchone()
    finally:
        conn.close()
    return bool(result and result[0] > 0)


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

    columns, cached_stats, row_count, profiled_at = await asyncio.gather(
        asyncio.to_thread(_get_column_schema, db_path, source, table),
        asyncio.to_thread(_get_cached_stats, project_root, source, table),
        asyncio.to_thread(_get_row_count, db_path, source, table),
        asyncio.to_thread(_get_profiled_at, project_root, source, table),
    )

    for col in columns:
        col["stats"] = cached_stats.get(col["name"])

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

    fresh_stats = await asyncio.to_thread(
        profile_table,
        project_root,
        source,
        table,
    )

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


# ---------------------------------------------------------------------------
# Lineage helpers (called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _build_lineage_dag() -> dict[str, Any] | None:
    """Build full DAG from dbt manifest (sources + models + edges).

    Returns:
        Dict with ``nodes`` and ``edges`` lists, or ``None`` if no manifest.
    """
    manifest = get_dbt_manifest()
    if manifest is None:
        return None

    nodes_out: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    depended_on_by: dict[str, list[str]] = {}
    test_map: dict[str, list[str]] = {}

    # Collect model dependencies and test associations
    for uid, node in manifest.get("nodes", {}).items():
        rtype = node.get("resource_type", "")
        if rtype == "model":
            deps = node.get("depends_on", {}).get("nodes", [])
            for dep in deps:
                depended_on_by.setdefault(dep, []).append(uid)
                edges.append({"source": dep, "target": uid})
        elif rtype == "test":
            for dep in node.get("depends_on", {}).get("nodes", []):
                test_map.setdefault(dep, []).append(node.get("name", uid))

    # Build node list — models
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "model":
            continue
        columns = node.get("columns", {})
        cols_documented = sum(1 for c in columns.values() if c.get("description"))
        deps = node.get("depends_on", {}).get("nodes", [])
        tests = test_map.get(uid, [])
        nodes_out.append(
            {
                "id": uid,
                "name": node.get("name", ""),
                "type": "model",
                "schema": node.get("schema", ""),
                "materialization": node.get("config", {}).get("materialized"),
                "description": node.get("description", ""),
                "depends_on": deps,
                "depended_on_by": depended_on_by.get(uid, []),
                "test_count": len(tests),
                "test_names": tests,
                "has_description": bool(node.get("description")),
                "columns_documented": cols_documented,
                "columns_total": len(columns),
            }
        )

    # Build node list — sources
    for uid, src in manifest.get("sources", {}).items():
        columns = src.get("columns", {})
        cols_documented = sum(1 for c in columns.values() if c.get("description"))
        tests = test_map.get(uid, [])
        nodes_out.append(
            {
                "id": uid,
                "name": src.get("name", ""),
                "type": "source",
                "schema": src.get("schema", ""),
                "materialization": None,
                "description": src.get("description", ""),
                "depends_on": [],
                "depended_on_by": depended_on_by.get(uid, []),
                "test_count": len(tests),
                "test_names": tests,
                "has_description": bool(src.get("description")),
                "columns_documented": cols_documented,
                "columns_total": len(columns),
            }
        )

    return {"nodes": nodes_out, "edges": edges}


def _get_impact_tree(
    reverse_map: dict[str, list[str]],
    node_id: str,
    all_nodes: dict[str, dict[str, Any]],
    visited: set[str] | None = None,
) -> dict[str, Any]:
    """Recursively build downstream impact tree from a node.

    Args:
        reverse_map: Mapping of node_id → list of downstream node_ids.
        node_id: Starting node unique_id.
        all_nodes: Combined dict of all manifest nodes + sources.
        visited: Set of already-visited node_ids for cycle detection.

    Returns:
        Tree dict with ``name``, ``type``, ``children``,
        ``total_downstream_count``, and optionally ``cycle``.
    """
    if visited is None:
        visited = set()

    node = all_nodes.get(node_id, {})
    if node_id in visited:
        return {
            "name": node.get("name", node_id),
            "type": node.get("resource_type", "unknown"),
            "cycle": True,
            "children": [],
            "total_downstream_count": 0,
        }

    visited.add(node_id)
    children = []
    total = 0
    for child_id in reverse_map.get(node_id, []):
        child_tree = _get_impact_tree(reverse_map, child_id, all_nodes, visited)
        children.append(child_tree)
        total += 1 + child_tree["total_downstream_count"]

    return {
        "name": node.get("name", node_id),
        "type": node.get("resource_type", "unknown"),
        "children": children,
        "total_downstream_count": total,
    }


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

    # Build combined node lookup and reverse map
    all_nodes: dict[str, dict[str, Any]] = {}
    reverse_map: dict[str, list[str]] = {}

    for uid, node in manifest.get("nodes", {}).items():
        all_nodes[uid] = node
        if node.get("resource_type") == "model":
            for dep in node.get("depends_on", {}).get("nodes", []):
                reverse_map.setdefault(dep, []).append(uid)

    for uid, src in manifest.get("sources", {}).items():
        all_nodes[uid] = src

    # Find the target model by name
    target_id: str | None = None
    for uid, node in all_nodes.items():
        if node.get("name") == model_name:
            target_id = uid
            break

    if target_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{model_name}' not found in dbt manifest.",
        )

    tree = _get_impact_tree(reverse_map, target_id, all_nodes)
    direct = reverse_map.get(target_id, [])
    node_info = all_nodes[target_id]

    return {
        "model": model_name,
        "type": node_info.get("resource_type", "unknown"),
        "direct_dependents": [all_nodes.get(d, {}).get("name", d) for d in direct],
        "tree": tree,
        "total_downstream_count": tree["total_downstream_count"],
    }
