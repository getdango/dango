"""tests/unit/test_utils_database.py

Tests for dango.utils.database — DuckDB schema initialization.
"""

from pathlib import Path

import duckdb
import pytest

from dango.utils.database import ensure_dbt_schemas


@pytest.mark.unit
class TestEnsureDbtSchemas:
    """Tests for ensure_dbt_schemas()."""

    def test_creates_file_when_not_exists(self, tmp_path: Path) -> None:
        """ensure_dbt_schemas creates DuckDB file when it doesn't exist (BUG-102)."""
        db_path = tmp_path / "data" / "warehouse.duckdb"
        assert not db_path.exists()

        ensure_dbt_schemas(db_path)

        assert db_path.exists()

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """ensure_dbt_schemas creates nested parent directories if needed (BUG-102)."""
        db_path = tmp_path / "deep" / "nested" / "data" / "warehouse.duckdb"
        assert not db_path.parent.exists()

        ensure_dbt_schemas(db_path)

        assert db_path.exists()

    def test_creates_all_four_schemas(self, tmp_path: Path) -> None:
        """ensure_dbt_schemas creates raw, staging, intermediate, and marts schemas."""
        db_path = tmp_path / "warehouse.duckdb"

        ensure_dbt_schemas(db_path)

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            schemas = [
                row[0]
                for row in conn.execute(
                    "SELECT schema_name FROM information_schema.schemata"
                ).fetchall()
            ]
        finally:
            conn.close()

        for expected in ("raw", "staging", "intermediate", "marts"):
            assert expected in schemas

    def test_idempotent_on_existing_file(self, tmp_path: Path) -> None:
        """ensure_dbt_schemas is safe to call on an existing database."""
        db_path = tmp_path / "warehouse.duckdb"

        # Create file with data first
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
        conn.execute("CREATE TABLE raw.test_table (id INTEGER)")
        conn.execute("INSERT INTO raw.test_table VALUES (42)")
        conn.close()

        # Re-run ensure_dbt_schemas — should not destroy existing data
        ensure_dbt_schemas(db_path)

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            result = conn.execute("SELECT * FROM raw.test_table").fetchall()
        finally:
            conn.close()

        assert result == [(42,)]

    def test_connection_closed_on_success(self, tmp_path: Path) -> None:
        """DuckDB connection is properly closed after successful schema creation."""
        db_path = tmp_path / "warehouse.duckdb"
        ensure_dbt_schemas(db_path)

        # If connection was not closed, this write would fail with a lock error
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE TABLE raw.verify_no_lock (id INTEGER)")
        conn.close()
