"""dango/web/routes/catalog.py

Data catalog API endpoints for column schema introspection and profiling.
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
from dango.validation import validate_source_name
from dango.web.helpers import get_project_root

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
    table = validate_source_name(table)
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


@router.get("/api/catalog/{source}/{table}/profile")
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
    table = validate_source_name(table)
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
