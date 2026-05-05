"""tests/unit/test_csv_schema_evolution.py

Unit tests for CSV schema evolution (--allow-schema-changes).
Tests the _validate_all_files_schema_match and _evolve_table_schema methods.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from dango.config.models import CSVSourceConfig
from dango.ingestion.csv_loader import CSVLoader


def _make_csv(directory: Path, filename: str, header: str, rows: list[str]) -> Path:
    """Write a CSV file with given header and rows."""
    filepath = directory / filename
    lines = [header] + rows
    filepath.write_text("\n".join(lines) + "\n")
    return filepath


@pytest.fixture()
def csv_env(tmp_path: Path) -> tuple[CSVLoader, Path, Path, CSVSourceConfig]:
    """Set up a CSV loader environment with DuckDB and a data directory."""
    db_path = tmp_path / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True)

    data_dir = tmp_path / "csv_data"
    data_dir.mkdir()

    loader = CSVLoader(tmp_path, db_path)
    config = CSVSourceConfig(directory=data_dir, file_pattern="*.csv")

    return loader, db_path, data_dir, config


@pytest.mark.unit
class TestStrictModeRejectsExtraColumns:
    """Default (strict) behavior rejects schema mismatches."""

    def test_extra_columns_rejected_without_flag(self, csv_env: Any) -> None:
        """Without --allow-schema-changes, extra columns cause an error."""
        loader, db_path, data_dir, config = csv_env

        # First file: id, name
        _make_csv(data_dir, "file1.csv", "id,name", ["1,Alice", "2,Bob"])

        # Load first file to establish table
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"
        assert result["total_rows"] == 2

        # Second file: id, name, email (extra column)
        _make_csv(data_dir, "file2.csv", "id,name,email", ["3,Charlie,c@x.com"])

        # Strict mode (default) — should fail
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "error"
        assert "Schema validation failed" in result.get("error", "")


@pytest.mark.unit
class TestAllowSchemaChangesExtraColumns:
    """--allow-schema-changes adds extra columns via ALTER TABLE."""

    def test_extra_columns_added_via_alter_table(self, csv_env: Any) -> None:
        """Extra columns in new files are added to existing table."""
        loader, db_path, data_dir, config = csv_env

        # First file: id, name
        _make_csv(data_dir, "file1.csv", "id,name", ["1,Alice", "2,Bob"])

        # Load to establish table
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"

        # Verify initial schema
        conn = duckdb.connect(str(db_path), read_only=True)
        cols = [
            row[0]
            for row in conn.execute("DESCRIBE raw_test_src.test_src").fetchall()
            if not row[0].startswith("_dango_")
        ]
        conn.close()
        assert "id" in cols
        assert "name" in cols
        assert "email" not in cols

        # Second file: id, name, email (extra column)
        _make_csv(data_dir, "file2.csv", "id,name,email", ["3,Charlie,c@x.com"])

        # Load with schema evolution enabled
        result = loader.load("test_src", config, "raw_test_src", allow_schema_changes=True)
        assert result["status"] == "success"

        # Verify new column exists
        conn = duckdb.connect(str(db_path), read_only=True)
        cols = [
            row[0]
            for row in conn.execute("DESCRIBE raw_test_src.test_src").fetchall()
            if not row[0].startswith("_dango_")
        ]
        conn.close()
        assert "email" in cols


@pytest.mark.unit
class TestAllowSchemaChangesMissingColumns:
    """--allow-schema-changes loads files with missing columns as NULL."""

    def test_missing_columns_loaded_as_null(self, csv_env: Any) -> None:
        """Files missing columns from existing table load with NULLs."""
        loader, db_path, data_dir, config = csv_env

        # First file: id, name, email
        _make_csv(data_dir, "file1.csv", "id,name,email", ["1,Alice,a@x.com", "2,Bob,b@x.com"])

        # Load to establish table with 3 columns
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"

        # Second file: id, name (missing email column)
        _make_csv(data_dir, "file2.csv", "id,name", ["3,Charlie"])

        # Load with schema evolution — missing column should be tolerated
        result = loader.load("test_src", config, "raw_test_src", allow_schema_changes=True)
        assert result["status"] == "success"

        # BUG-168: Verify data was actually loaded with NULLs (not just no error)
        assert result["total_rows"] == 3  # 2 from file1 + 1 from file2

        conn = duckdb.connect(str(db_path), read_only=True)
        rows = conn.execute(
            "SELECT id, name, email FROM raw_test_src.test_src ORDER BY id"
        ).fetchall()
        conn.close()

        # file2's row (id=3) should have email=NULL
        assert rows[2][0] == 3  # id
        assert rows[2][1] == "Charlie"  # name
        assert rows[2][2] is None  # email is NULL


@pytest.mark.unit
class TestZeroLoadedRowsReturnsError:
    """BUG-169: sync returns error when all file loads produce 0 rows."""

    def test_empty_file_only_returns_error(self, csv_env: Any) -> None:
        """CSV with header-only (0 rows) returns status error."""
        loader, db_path, data_dir, config = csv_env

        # First load: establish table with real data
        _make_csv(data_dir, "file1.csv", "id,name", ["1,Alice"])
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"

        # Add a header-only file (0 rows) — file1 stays unchanged
        _make_csv(data_dir, "file2.csv", "id,name", [])

        # Second load: file2 has 0 rows (skipped), file1 unchanged → no new loads
        # But file1 is "unchanged" so files_attempted = 1 (just file2)
        # and files_loaded = 0 → error
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "error"
        assert "No data loaded" in result["error"]

    def test_deletion_only_is_success(self, csv_env: Any) -> None:
        """File deleted from disk → sync detects deletion → status success."""
        loader, db_path, data_dir, config = csv_env

        # Load a file to establish table
        _make_csv(data_dir, "file1.csv", "id,name", ["1,Alice"])
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"

        # Delete the file from disk
        (data_dir / "file1.csv").unlink()

        # Sync should succeed (deletion detected)
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"
        assert result["deleted"] == 1


@pytest.mark.unit
class TestSchemaEvolutionTriggersDrift:
    """BUG-170: drift detection fires after CSV schema evolution."""

    def test_column_added_detected_after_schema_evolution(self, csv_env: Any) -> None:
        """After schema evolution adds a column, drift detection finds column_added."""
        from dango.governance.schema_drift import detect_table_drift

        loader, db_path, data_dir, config = csv_env
        project_root = loader.project_root

        # Step 1: Load CSV with columns id, name → establish table
        _make_csv(data_dir, "file1.csv", "id,name", ["1,Alice", "2,Bob"])
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"

        # Step 2: First drift call establishes baseline (returns [])
        # detect_table_drift uses project_root / "data" / "warehouse.duckdb"
        # and project_root / ".dango" / "dango.db" — both exist via csv_env fixture
        events = detect_table_drift(project_root, "test_src", "test_src")
        assert events == []

        # Step 3: Load CSV with id, name, email using allow_schema_changes
        _make_csv(data_dir, "file2.csv", "id,name,email", ["3,Charlie,c@x.com"])
        result = loader.load("test_src", config, "raw_test_src", allow_schema_changes=True)
        assert result["status"] == "success"

        # Step 4: Drift detection should find column_added for "email"
        events = detect_table_drift(project_root, "test_src", "test_src")

        assert len(events) >= 1
        added_events = [e for e in events if e["event_type"] == "column_added"]
        assert len(added_events) == 1
        assert added_events[0]["column_name"] == "email"
        assert added_events[0]["severity"] == "additive"
