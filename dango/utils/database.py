"""
Database utilities for Dango projects
"""

from pathlib import Path
import duckdb


def ensure_dbt_schemas(duckdb_path: Path) -> None:
    """
    Ensure all dbt schemas exist in DuckDB database.

    Creates schemas upfront so they're visible in Metabase even before
    any models are created in them.

    Args:
        duckdb_path: Path to DuckDB database file
    """
    if not duckdb_path.exists():
        return

    conn = duckdb.connect(str(duckdb_path))

    # Create all dbt schemas
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    conn.execute("CREATE SCHEMA IF NOT EXISTS staging")
    conn.execute("CREATE SCHEMA IF NOT EXISTS intermediate")
    conn.execute("CREATE SCHEMA IF NOT EXISTS marts")

    conn.close()
