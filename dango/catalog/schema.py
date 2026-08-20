"""dango/catalog/schema.py

DuckDB introspection helpers for column schema, row counts, and table discovery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


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
            "WHERE table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position",
            [schema, table],
        ).fetchall()
    finally:
        conn.close()

    return [{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in rows]


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
            "WHERE table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position",
            [schema, table],
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()

    return [{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in rows]


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
