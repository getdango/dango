"""dango/web/routes/catalog.py

Data catalog API endpoints for column schema introspection, profiling,
lineage, impact analysis, and unified model browsing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query

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
    conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
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
    conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
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
    conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
    try:
        result = conn.execute(
            "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = ?",
            [schema],
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
    conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
    try:
        result = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = ? AND table_name = ? "
            "AND table_name NOT LIKE '_dlt_%'",
            [schema, table],
        ).fetchone()
    finally:
        conn.close()
    return bool(result and result[0] > 0)


def _get_raw_tables_from_duckdb(db_path: Path) -> list[dict[str, str]]:
    """List all user tables in raw_* schemas (for discovering unmodeled tables).

    Args:
        db_path: Path to the DuckDB warehouse file.

    Returns:
        List of ``{"schema": ..., "table": ..., "source_name": ...}``.
    """
    conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
    try:
        rows = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema LIKE 'raw_%' AND table_name NOT LIKE '_dlt_%'"
        ).fetchall()
    finally:
        conn.close()
    return [{"schema": r[0], "table": r[1], "source_name": r[0][4:]} for r in rows]


def _get_source_summary_stats(db_path: Path) -> dict[str, dict[str, int]]:
    """Query per-source table counts and estimated row counts.

    Args:
        db_path: Path to DuckDB warehouse file.

    Returns:
        ``{source_name: {"table_count": int, "estimated_row_total": int}}``.
    """
    try:
        conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
        try:
            rows = conn.execute(
                "SELECT schema_name, COUNT(*) AS table_count, "
                "COALESCE(SUM(estimated_size), 0) AS estimated_row_total "
                "FROM duckdb_tables() "
                "WHERE schema_name LIKE 'raw_%' "
                "AND table_name NOT LIKE '_dlt_%' "
                "GROUP BY schema_name"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("source_summary_stats_failed", db_path=str(db_path))
        return {}
    return {r[0][4:]: {"table_count": r[1], "estimated_row_total": r[2]} for r in rows}


# ---------------------------------------------------------------------------
# Manifest / model helpers (called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _get_run_results() -> dict[str, Any] | None:
    """Load ``dbt/target/run_results.json``.

    Returns:
        Parsed dict or ``None`` if the file does not exist.
    """
    path = get_project_root() / "dbt" / "target" / "run_results.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            return result
    except Exception:
        logger.warning("Failed to parse run_results.json")
        return None


def _build_test_status_map(
    manifest: dict[str, Any],
    run_results: dict[str, Any] | None,
) -> dict[str, list[dict[str, str | None]]]:
    """Map model/source unique_ids to their test results.

    Args:
        manifest: Parsed dbt manifest.
        run_results: Parsed ``run_results.json`` or ``None``.

    Returns:
        ``{model_unique_id: [{"name": ..., "status": "pass"|"fail"|"error"|None}]}``.
    """
    # Build status lookup from run_results
    result_status: dict[str, str] = {}
    if run_results:
        for r in run_results.get("results", []):
            uid = r.get("unique_id", "")
            status = r.get("status", "")
            if uid and status:
                result_status[uid] = status

    test_map: dict[str, list[dict[str, str | None]]] = {}
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "test":
            continue
        test_name = node.get("name", uid)
        status = result_status.get(uid)
        for dep in node.get("depends_on", {}).get("nodes", []):
            test_map.setdefault(dep, []).append({"name": test_name, "status": status})

    return test_map


def _classify_model_type(node: dict[str, Any]) -> str:
    """Classify a dbt model as ``staging``, ``intermediate``, or ``marts``.

    Classification priority:
    1. Schema name: ``staging`` / ``intermediate`` / ``marts``.
    2. Name prefix: ``stg_`` → staging, ``fct_`` / ``dim_`` → marts,
       ``int_`` → intermediate.
    3. Fallback: ``intermediate``.

    Args:
        node: A manifest model node dict.

    Returns:
        One of ``"staging"``, ``"intermediate"``, ``"marts"``.
    """
    schema = (node.get("schema") or "").lower()
    if schema == "staging":
        return "staging"
    if schema == "intermediate":
        return "intermediate"
    if schema == "marts":
        return "marts"

    name = (node.get("name") or "").lower()
    if name.startswith("stg_"):
        return "staging"
    if name.startswith(("fct_", "dim_")):
        return "marts"
    if name.startswith("int_"):
        return "intermediate"

    return "intermediate"


def _get_model_column_schema(
    db_path: Path,
    schema: str,
    table: str,
) -> list[dict[str, Any]]:
    """Query DuckDB ``information_schema.columns`` for any schema.

    Unlike :func:`_get_column_schema`, this is not restricted to
    ``raw_*`` schemas — it works for staging, intermediate, and marts.

    Args:
        db_path: Path to the DuckDB warehouse file.
        schema: Schema name (e.g. ``staging``, ``marts``).
        table: Table/view name.

    Returns:
        List of ``{"name": ..., "type": ..., "nullable": bool}``,
        or empty list if the table does not exist.
    """
    conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
    try:
        rows = conn.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
            "ORDER BY ordinal_position"
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()

    return [{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in rows]


def _build_catalog_models(
    manifest: dict[str, Any],
    run_results: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the full catalog models + sources response from manifest.

    Args:
        manifest: Parsed dbt manifest.
        run_results: Parsed run_results or ``None``.

    Returns:
        Dict with ``models`` and ``sources`` lists.
    """
    test_map = _build_test_status_map(manifest, run_results)

    models: list[dict[str, Any]] = []
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "model":
            continue
        columns = node.get("columns", {})
        cols_documented = sum(1 for c in columns.values() if c.get("description"))
        tests = test_map.get(uid, [])
        tests_passing = sum(1 for t in tests if t["status"] == "pass")
        tests_failing = sum(1 for t in tests if t["status"] in ("fail", "error"))

        models.append(
            {
                "unique_id": uid,
                "name": node.get("name", ""),
                "type": _classify_model_type(node),
                "schema": node.get("schema", ""),
                "materialization": node.get("config", {}).get("materialized", "view"),
                "description": node.get("description", ""),
                "test_count": len(tests),
                "tests_passing": tests_passing,
                "tests_failing": tests_failing,
                "columns_total": len(columns),
                "columns_documented": cols_documented,
                "tags": node.get("tags", []),
            }
        )

    # Sort: type order (staging → intermediate → marts), then name
    type_order = {"staging": 0, "intermediate": 1, "marts": 2}
    models.sort(key=lambda m: (type_order.get(m["type"], 1), m["name"].lower()))

    sources: list[dict[str, Any]] = []
    for uid, src in manifest.get("sources", {}).items():
        columns = src.get("columns", {})
        cols_documented = sum(1 for c in columns.values() if c.get("description"))
        tests = test_map.get(uid, [])

        sources.append(
            {
                "unique_id": uid,
                "name": src.get("name", ""),
                "type": "source",
                "schema": src.get("schema", ""),
                "description": src.get("description", ""),
                "source_name": src.get("source_name", ""),
                "test_count": len(tests),
                "columns_total": len(columns),
                "columns_documented": cols_documented,
            }
        )

    sources.sort(key=lambda s: s["name"].lower())

    return {"models": models, "sources": sources}


def _find_model_in_manifest(
    manifest: dict[str, Any],
    model_name: str,
) -> tuple[str | None, dict[str, Any] | None, str]:
    """Find a model or source by name in the manifest.

    Models are preferred over sources when names collide.

    Args:
        manifest: Parsed dbt manifest.
        model_name: Name to search for.

    Returns:
        Tuple of ``(unique_id, node_dict, kind)`` where *kind* is
        ``"model"`` or ``"source"``.  Returns ``(None, None, "")``
        if not found.
    """
    # Search models first
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") == "model" and node.get("name") == model_name:
            return uid, node, "model"
    # Then sources
    for uid, src in manifest.get("sources", {}).items():
        if src.get("name") == model_name:
            return uid, src, "source"
    return None, None, ""


def _build_model_detail(
    manifest: dict[str, Any],
    run_results: dict[str, Any] | None,
    target_uid: str,
    target_node: dict[str, Any],
    kind: str,
    db_columns: list[dict[str, Any]],
    profiled_at: str | None,
) -> dict[str, Any]:
    """Build the detail response for a single model or source.

    Args:
        manifest: Full dbt manifest.
        run_results: Parsed run_results or ``None``.
        target_uid: The unique_id of the target.
        target_node: The manifest node dict.
        kind: ``"model"`` or ``"source"``.
        db_columns: Column schema from DuckDB (may be empty).
        profiled_at: Last profiling timestamp or ``None``.

    Returns:
        Full detail response dict.
    """
    test_map = _build_test_status_map(manifest, run_results)
    model_tests = test_map.get(target_uid, [])
    manifest_columns = target_node.get("columns", {})

    # Build reverse dependency map
    reverse_map: dict[str, list[str]] = {}
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") == "model":
            for dep in node.get("depends_on", {}).get("nodes", []):
                reverse_map.setdefault(dep, []).append(uid)

    # Merge DuckDB columns with manifest column descriptions
    model_name = target_node.get("name", "")
    columns: list[dict[str, Any]] = []
    if db_columns:
        for col in db_columns:
            manifest_col = manifest_columns.get(col["name"], {})
            # Map tests to this column by dbt naming convention:
            # test names follow {test_type}_{model}_{column} pattern
            col_suffix = f"_{model_name}_{col['name']}"
            col_tests = [t for t in model_tests if t["name"] and t["name"].endswith(col_suffix)]
            columns.append(
                {
                    "name": col["name"],
                    "type": col["type"],
                    "nullable": col["nullable"],
                    "description": manifest_col.get("description") or None,
                    "tests": col_tests if col_tests else None,
                    "stats": None,
                }
            )
    else:
        # Model not materialised — show manifest columns without type info
        for col_name, col_info in manifest_columns.items():
            columns.append(
                {
                    "name": col_name,
                    "type": None,
                    "nullable": None,
                    "description": col_info.get("description") or None,
                    "tests": None,
                    "stats": None,
                }
            )

    result: dict[str, Any] = {
        "unique_id": target_uid,
        "name": model_name,
        "schema": target_node.get("schema", ""),
        "description": target_node.get("description", ""),
        "tags": target_node.get("tags", []),
        "meta": target_node.get("meta", {}),
        "columns": columns,
        "depends_on": target_node.get("depends_on", {}).get("nodes", []),
        "depended_on_by": reverse_map.get(target_uid, []),
        "tests": model_tests if model_tests else None,
        "row_count": None,
        "profiled_at": profiled_at,
    }

    if kind == "model":
        result["type"] = _classify_model_type(target_node)
        result["materialization"] = target_node.get("config", {}).get("materialized", "view")
        result["raw_code"] = target_node.get("raw_code") or target_node.get("raw_sql")
        result["compiled_code"] = target_node.get("compiled_code") or target_node.get(
            "compiled_sql"
        )
    else:
        result["type"] = "source"
        result["materialization"] = None
        result["raw_code"] = None
        result["compiled_code"] = None
        result["source_name"] = target_node.get("source_name", "")

    return result


def _search_manifest(
    manifest: dict[str, Any],
    query: str,
) -> list[dict[str, Any]]:
    """Search manifest models and sources by name, description, column names.

    Args:
        manifest: Parsed dbt manifest.
        query: Case-insensitive search string.

    Returns:
        List of search result dicts (max 50).
    """
    q = query.lower()
    name_matches: list[dict[str, Any]] = []
    desc_matches: list[dict[str, Any]] = []
    col_matches: list[dict[str, Any]] = []

    # Search models
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "model":
            continue
        name = node.get("name", "")
        desc = node.get("description", "")
        model_type = _classify_model_type(node)

        if q in name.lower():
            name_matches.append(
                {
                    "unique_id": uid,
                    "name": name,
                    "type": model_type,
                    "description": desc,
                    "match_type": "name",
                }
            )
            continue

        if desc and q in desc.lower():
            desc_matches.append(
                {
                    "unique_id": uid,
                    "name": name,
                    "type": model_type,
                    "description": desc,
                    "match_type": "description",
                }
            )
            continue

        for col_name in node.get("columns", {}):
            if q in col_name.lower():
                col_matches.append(
                    {
                        "unique_id": uid,
                        "name": name,
                        "type": model_type,
                        "description": desc,
                        "match_type": "column",
                        "matched_column": col_name,
                    }
                )
                break

    # Search sources
    for uid, src in manifest.get("sources", {}).items():
        name = src.get("name", "")
        desc = src.get("description", "")

        if q in name.lower():
            name_matches.append(
                {
                    "unique_id": uid,
                    "name": name,
                    "type": "source",
                    "description": desc,
                    "match_type": "name",
                }
            )
            continue

        if desc and q in desc.lower():
            desc_matches.append(
                {
                    "unique_id": uid,
                    "name": name,
                    "type": "source",
                    "description": desc,
                    "match_type": "description",
                }
            )
            continue

        for col_name in src.get("columns", {}):
            if q in col_name.lower():
                col_matches.append(
                    {
                        "unique_id": uid,
                        "name": name,
                        "type": "source",
                        "description": desc,
                        "match_type": "column",
                        "matched_column": col_name,
                    }
                )
                break

    results = name_matches + desc_matches + col_matches
    return results[:50]


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

    # BUG-132: Discover raw tables without dbt staging models
    project_root = get_project_root()
    db_path = project_root / "data" / "warehouse.duckdb"
    source_stats: dict[str, dict[str, int]] = {}
    if db_path.exists():
        raw_tables, source_stats = await asyncio.gather(
            asyncio.to_thread(_get_raw_tables_from_duckdb, db_path),
            asyncio.to_thread(_get_source_summary_stats, db_path),
        )
        known_names = {s["name"] for s in result["sources"]}
        for rt in raw_tables:
            if rt["table"] not in known_names:
                result["sources"].append(
                    {
                        "unique_id": f"raw.{rt['schema']}.{rt['table']}",
                        "name": rt["table"],
                        "type": "source",
                        "schema": rt["schema"],
                        "description": "",
                        "source_name": rt["source_name"],
                        "test_count": 0,
                        "columns_total": 0,
                        "columns_documented": 0,
                    }
                )
        result["sources"].sort(key=lambda s: s["name"].lower())

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

    # For source tables, try to get profiled_at
    if kind == "source":
        source_name = target_node.get("source_name", "")
        if source_name:
            profiled_at = await asyncio.to_thread(
                _get_profiled_at, project_root, source_name, table
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

    # BUG-134: Inject cached profiling stats for source tables
    if kind == "source" and source_name and result.get("columns"):
        cached_stats = await asyncio.to_thread(_get_cached_stats, project_root, source_name, table)
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
                "source_name": src.get("source_name", ""),
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
    ancestors: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Recursively build downstream impact tree from a node.

    Uses an ancestor chain (not a shared visited set) so diamond dependencies
    expand fully under each branch while true back-edge cycles are detected.

    Args:
        reverse_map: Mapping of node_id → list of downstream node_ids.
        node_id: Starting node unique_id.
        all_nodes: Combined dict of all manifest nodes + sources.
        ancestors: Immutable set of ancestor node_ids for cycle detection.

    Returns:
        Tree dict with ``name``, ``type``, ``children``,
        ``total_downstream_count``, and optionally ``cycle``.
    """
    if ancestors is None:
        ancestors = frozenset()

    node = all_nodes.get(node_id, {})
    if node_id in ancestors:
        return {
            "name": node.get("name", node_id),
            "type": node.get("resource_type", "unknown"),
            "cycle": True,
            "children": [],
            "total_downstream_count": 0,
        }

    new_ancestors = ancestors | {node_id}
    children = []
    total = 0
    for child_id in reverse_map.get(node_id, []):
        child_tree = _get_impact_tree(reverse_map, child_id, all_nodes, new_ancestors)
        children.append(child_tree)
        total += 1 + child_tree["total_downstream_count"]

    return {
        "name": node.get("name", node_id),
        "type": node.get("resource_type", "unknown"),
        "children": children,
        "total_downstream_count": total,
    }


def _build_impact_response(
    manifest: dict[str, Any],
    model_name: str,
) -> dict[str, Any] | None:
    """Build impact analysis response from manifest (blocking helper).

    Searches models first, then sources, so models take priority when
    names collide (common in dbt: source "orders" + model "orders").

    Args:
        manifest: Parsed dbt manifest dict.
        model_name: Validated model/source name to look up.

    Returns:
        Impact response dict, or ``None`` if *model_name* not found.
    """
    all_nodes: dict[str, dict[str, Any]] = {}
    reverse_map: dict[str, list[str]] = {}

    for uid, node in manifest.get("nodes", {}).items():
        all_nodes[uid] = node
        if node.get("resource_type") == "model":
            for dep in node.get("depends_on", {}).get("nodes", []):
                reverse_map.setdefault(dep, []).append(uid)

    for uid, src in manifest.get("sources", {}).items():
        all_nodes[uid] = src

    # Find target — prefer models over sources for name collisions
    target_id: str | None = None
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") == "model" and node.get("name") == model_name:
            target_id = uid
            break
    if target_id is None:
        for uid in manifest.get("sources", {}):
            if all_nodes[uid].get("name") == model_name:
                target_id = uid
                break

    if target_id is None:
        return None

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
