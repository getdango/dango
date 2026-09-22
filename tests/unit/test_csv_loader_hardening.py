"""tests/unit/test_csv_loader_hardening.py

Unit tests for csv_loader.py hardening (1.0.8-AE): the --allow-schema-changes
type-check gap and unescaped filepath/filename SQL interpolation.
"""

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
    """Set up a CSV loader environment with DuckDB and a data directory.

    Mirrors the fixture in tests/unit/test_csv_loader.py and
    tests/unit/test_csv_schema_evolution.py — a real (not mocked) DuckDB file +
    real CSV files on disk, exercised through CSVLoader.load(). A real connection
    is required here because the type-validation logic under test relies on
    genuine DuckDB type inference (read_csv_auto) and TRY_CAST semantics, which a
    mocked connection cannot meaningfully reproduce.
    """
    db_path = tmp_path / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True)

    data_dir = tmp_path / "csv_data"
    data_dir.mkdir()

    loader = CSVLoader(tmp_path, db_path)
    config = CSVSourceConfig(directory=data_dir, file_pattern="*.csv")

    return loader, db_path, data_dir, config


@pytest.mark.unit
class TestAllowSchemaChangesTypeChecking:
    """Tests for the type check _validate_all_files_schema_match() now runs when
    allow_schema_changes=True (Fix 1) — previously this path (the one dango's own
    error messages recommend) had zero type protection: a type-incompatible file
    would silently be skipped instead of raising a clear error.
    """

    def test_allow_schema_changes_raises_on_type_mismatch(self, csv_env: Any) -> None:
        """A non-numeric value landing in a locked-in BIGINT column must still raise
        CSVSchemaMismatchError under allow_schema_changes=True — previously this
        silently skipped the file instead (the bare except in _load_new_file, which
        is never reached because skip_schema_check=True suppresses _validate_schema_match
        entirely on this path).
        """
        loader, _db_path, data_dir, config = csv_env

        # First file establishes the table with 'amount' inferred as BIGINT.
        _make_csv(data_dir, "file1.csv", "id,amount", ["1,100", "2,200"])
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"

        # Second file has matching column NAMES but a non-numeric 'amount' value —
        # cannot cast to BIGINT. Only reachable via allow_schema_changes=True since
        # _validate_schema_match() is skipped on this path.
        _make_csv(data_dir, "file2.csv", "id,amount", ["3,not_a_number"])
        result = loader.load("test_src", config, "raw_test_src", allow_schema_changes=True)

        assert result["status"] == "error"
        assert "amount" in result["error"]
        assert "BIGINT" in result["error"]

    def test_allow_schema_changes_still_allows_new_columns(self, csv_env: Any) -> None:
        """Existing schema-evolution behavior (new columns added via ALTER TABLE) is
        unchanged by Fix 1's added type check — a file with a genuinely new column
        (and otherwise type-compatible existing columns) still loads successfully.
        """
        loader, db_path, data_dir, config = csv_env

        # First file: id, amount (amount infers as BIGINT).
        _make_csv(data_dir, "file1.csv", "id,amount", ["1,100", "2,200"])
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"

        # Second file: same 'amount' column with compatible BIGINT values, plus a
        # genuinely new 'region' column.
        _make_csv(data_dir, "file2.csv", "id,amount,region", ["3,300,west"])
        result = loader.load("test_src", config, "raw_test_src", allow_schema_changes=True)

        assert result["status"] == "success"
        assert result["new"] == 1

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            cols = [
                row[0]
                for row in conn.execute("DESCRIBE raw_test_src.test_src").fetchall()
                if not row[0].startswith("_dango_")
            ]
        finally:
            conn.close()
        assert "region" in cols

    def test_allow_schema_changes_missing_column_not_flagged_as_type_mismatch(
        self, csv_env: Any
    ) -> None:
        """A column entirely absent from the file (not merely type-incompatible)
        must not be flagged by the new type check as a mismatch — it's a
        missing-column case, tolerated elsewhere by NULL-padding.

        Regression test for a bug found while implementing Fix 1: the type-check
        loop originally compared every table column against
        `file_types.get(col)`, so a column missing from the file entirely (not
        just a different type) looked "mismatched" and fell into the same
        per-column TRY_CAST probe as a real type mismatch. That probe SELECTs the
        column by name from the file — which doesn't exist there — raising a
        DuckDB Binder Error that got caught and misreported as "could not verify
        column type compatibility", turning an ordinary, previously-supported
        missing-column load into a false failure. This is the same scenario
        `tests/unit/test_csv_schema_evolution.py::TestAllowSchemaChangesMissingColumns::
        test_missing_columns_loaded_as_null` already covers end-to-end; this test
        pins it specifically against the new type-check code path this task adds.
        """
        loader, db_path, data_dir, config = csv_env

        # First file: id, name, email (3 columns).
        _make_csv(data_dir, "file1.csv", "id,name,email", ["1,Alice,a@x.com", "2,Bob,b@x.com"])
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"

        # Second file: id, name only — 'email' is entirely missing, not
        # type-incompatible.
        _make_csv(data_dir, "file2.csv", "id,name", ["3,Charlie"])
        result = loader.load("test_src", config, "raw_test_src", allow_schema_changes=True)

        assert result["status"] == "success"

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = conn.execute(
                "SELECT id, name, email FROM raw_test_src.test_src ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        assert rows[-1] == (3, "Charlie", None)


@pytest.mark.unit
class TestFilepathEscaping:
    """Tests for _sql_quote() filepath/filename escaping (Fix 2) — a folder or file
    name containing an apostrophe must not break the SQL string literals
    csv_loader.py interpolates filepaths/filenames into.
    """

    def test_filepath_with_apostrophe_loads_successfully(self, tmp_path: Path) -> None:
        """A directory containing a literal apostrophe in its name loads successfully.

        Live-verified pre-fix: temporarily reverted the _sql_quote() escaping (Fix 2)
        and re-ran this exact test — it raised `Parser Error: syntax error at or near
        "s"` (DuckDB's ParserException, wrapped by _get_file_columns's own
        `except Exception` into "Failed to read file schema from ..."), uncaught
        directly out of loader.load() — the same malformed-quoted-literal failure
        described in the task doc's live repro ("...client's_data" ->
        `syntax error at or near "s_data"`; this test's directory name produces the
        equivalent break at the `'s` boundary). Restored the fix and re-ran: passes
        as asserted below.
        """
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True)

        data_dir = tmp_path / "client's data"
        data_dir.mkdir()

        _make_csv(data_dir, "file1.csv", "id,amount", ["1,100", "2,200"])

        loader = CSVLoader(tmp_path, db_path)
        config = CSVSourceConfig(directory=data_dir, file_pattern="*.csv")

        result = loader.load("test_src", config, "raw_test_src")

        assert result["status"] == "success"
        assert result["new"] == 1
        assert result["total_rows"] == 2

    def test_filepath_without_apostrophe_unaffected(self, tmp_path: Path) -> None:
        """A normal filepath (no apostrophe) still loads identically before/after
        the fix — _sql_quote() is a no-op when there's no embedded quote to escape.
        """
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True)

        data_dir = tmp_path / "csv_data"
        data_dir.mkdir()

        _make_csv(data_dir, "file1.csv", "id,amount", ["1,100", "2,200"])

        loader = CSVLoader(tmp_path, db_path)
        config = CSVSourceConfig(directory=data_dir, file_pattern="*.csv")

        result = loader.load("test_src", config, "raw_test_src")

        assert result["status"] == "success"
        assert result["new"] == 1
        assert result["total_rows"] == 2
