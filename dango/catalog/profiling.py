"""dango/catalog/profiling.py

Cached profiling stats readers from SQLite (.dango/dango.db).
"""

from __future__ import annotations

from pathlib import Path

from dango.utils.dango_db import connect


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
            "FROM profiling_stats WHERE source = ? AND table_name = ? "
            "AND column_name != '__row_count__'",
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


def _get_cached_row_counts(project_root: Path) -> dict[tuple[str, str], int]:
    """Read all cached table-level row counts from SQLite.

    Args:
        project_root: Dango project root.

    Returns:
        Mapping of ``{(source, table_name): row_count}`` for every table that
        has a cached ``__row_count__`` synthetic stat row.
    """
    result: dict[tuple[str, str], int] = {}
    with connect(project_root) as conn:
        rows = conn.execute(
            "SELECT source, table_name, stat_value FROM profiling_stats "
            "WHERE column_name = '__row_count__' AND stat_type = 'value'"
        ).fetchall()
    for row in rows:
        try:
            result[(row[0], row[1])] = int(row[2])
        except (TypeError, ValueError):
            continue
    return result
