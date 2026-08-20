"""dango/catalog/models.py

Response builders for catalog models/sources — depends on manifest.py and profiling.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from dango.catalog.manifest import (
    _classify_model_type,
    _model_profiling_key,
)
from dango.catalog.profiling import _get_cached_row_counts
from dango.logging import get_logger

logger = get_logger(__name__)


def _get_run_results() -> dict[str, Any] | None:
    """Load ``dbt/target/run_results.json``.

    Returns:
        Parsed dict or ``None`` if the file does not exist.
    """
    from dango.web.helpers import get_project_root

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
    from dango.catalog.manifest import _build_test_status_map
    from dango.utils.dbt_status import get_model_statuses
    from dango.web.helpers import get_project_root

    test_map = _build_test_status_map(manifest, run_results)

    project_root = get_project_root()
    model_statuses = get_model_statuses(project_root)
    cached_row_counts = _get_cached_row_counts(project_root)

    models: list[dict[str, Any]] = []
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "model":
            continue
        columns = node.get("columns", {})
        cols_documented = sum(1 for c in columns.values() if c.get("description"))
        tests = test_map.get(uid, [])
        tests_passing = sum(1 for t in tests if t["status"] == "pass")
        tests_warning = sum(1 for t in tests if t["status"] == "warn")
        tests_failing = sum(1 for t in tests if t["status"] in ("fail", "error"))

        status_info = model_statuses.get(uid, {})
        key = _model_profiling_key(node, "model")
        row_count = cached_row_counts.get(key) if key else None

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
                "tests_warning": tests_warning,
                "tests_failing": tests_failing,
                "columns_total": len(columns),
                "columns_documented": cols_documented,
                "tags": node.get("tags", []),
                "last_run": status_info.get("last_run"),
                "status": status_info.get("status"),
                "row_count": row_count,
            }
        )

    # Seeds — resource_type == "seed" nodes from manifest.
    # Note: seeds don't have dbt tests (test_count=0, always) or status tracking.
    # Row counts are populated from profiling cache if available.
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "seed":
            continue
        columns = node.get("columns", {})
        cols_documented = sum(1 for c in columns.values() if c.get("description"))
        key = _model_profiling_key(node, "seed")
        row_count = cached_row_counts.get(key) if key else None
        models.append(
            {
                "unique_id": uid,
                "name": node.get("name", ""),
                "type": "seed",
                "schema": node.get("schema", ""),
                "materialization": "table",
                "description": node.get("description", ""),
                "test_count": 0,  # Seeds don't have dbt tests
                "tests_passing": 0,
                "tests_warning": 0,
                "tests_failing": 0,
                "columns_total": len(columns),
                "columns_documented": cols_documented,
                "tags": node.get("tags", []),
                "last_run": None,  # Seeds don't have execution status
                "status": None,
                "row_count": row_count,
            }
        )

    # Sort: type order (staging → intermediate → marts → seed), then name
    type_order = {"staging": 0, "intermediate": 1, "marts": 2, "seed": 3}
    models.sort(key=lambda m: (type_order.get(m["type"], 1), m["name"].lower()))

    sources: list[dict[str, Any]] = []
    for uid, src in manifest.get("sources", {}).items():
        columns = src.get("columns", {})
        cols_documented = sum(1 for c in columns.values() if c.get("description"))
        tests = test_map.get(uid, [])
        tests_passing = sum(1 for t in tests if t["status"] == "pass")
        tests_warning = sum(1 for t in tests if t["status"] == "warn")
        tests_failing = sum(1 for t in tests if t["status"] in ("fail", "error"))

        key = _model_profiling_key(src, "source")
        row_count = cached_row_counts.get(key) if key else None

        sources.append(
            {
                "unique_id": uid,
                "name": src.get("name", ""),
                "type": "source",
                "schema": src.get("schema", ""),
                "description": src.get("description", ""),
                "source_name": src.get("source_name", ""),
                "test_count": len(tests),
                "tests_passing": tests_passing,
                "tests_warning": tests_warning,
                "tests_failing": tests_failing,
                "columns_total": len(columns),
                "columns_documented": cols_documented,
                "row_count": row_count,
            }
        )

    sources.sort(key=lambda s: s["name"].lower())

    return {"models": models, "sources": sources}


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
    from dango.catalog.manifest import _build_test_status_map

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
