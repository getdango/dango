"""dango/utils/query_connection.py

Locked-down, read-only DuckDB connection for user- and agent-supplied SQL.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb


def open_locked_query_connection(
    db_path: Path | str, *, memory_limit: str = "1GB"
) -> duckdb.DuckDBPyConnection:
    """Open an in-memory connection with *db_path* attached READ_ONLY, then lock it."""
    import duckdb

    path = Path(db_path)
    # Attach under the file stem ("warehouse"): dbt-created views carry
    # catalog-qualified bodies ("warehouse"."schema"."t") and fail to bind under any other alias.
    alias = path.stem.replace('"', '""')
    literal = str(path).replace("'", "''")
    conn = duckdb.connect(":memory:")
    try:
        conn.execute(f"ATTACH '{literal}' AS \"{alias}\" (READ_ONLY)")
        conn.execute(f'USE "{alias}"')
        conn.execute(f"SET memory_limit='{memory_limit}'")
        conn.execute("SET enable_external_access=false")
        conn.execute("SET lock_configuration=true")
    except Exception:
        conn.close()
        raise
    return conn
