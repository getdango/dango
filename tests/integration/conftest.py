"""Integration test fixtures."""

import pytest
import duckdb


@pytest.fixture
def test_duckdb(tmp_path):
    """Create a temporary DuckDB database with standard Dango schemas.

    Returns the path to the database file.
    """
    db_path = tmp_path / "test_warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
        conn.execute("CREATE SCHEMA IF NOT EXISTS staging")
        conn.execute("CREATE SCHEMA IF NOT EXISTS intermediate")
        conn.execute("CREATE SCHEMA IF NOT EXISTS marts")
    finally:
        conn.close()
    return db_path
