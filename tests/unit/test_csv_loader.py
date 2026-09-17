"""tests/unit/test_csv_loader.py

Tests for dango.ingestion.csv_loader — multi-format file loading.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from dango.config.models import CSVSourceConfig, LocalFilesSourceConfig
from dango.ingestion.csv_loader import SUPPORTED_READ_FUNCTIONS, CSVLoader


@pytest.mark.unit
class TestSupportedReadFunctions:
    """Tests for the SUPPORTED_READ_FUNCTIONS constant."""

    def test_csv_extension(self) -> None:
        assert SUPPORTED_READ_FUNCTIONS[".csv"] == "read_csv_auto"

    def test_json_extension(self) -> None:
        assert SUPPORTED_READ_FUNCTIONS[".json"] == "read_json_auto"

    def test_jsonl_extension(self) -> None:
        assert SUPPORTED_READ_FUNCTIONS[".jsonl"] == "read_json_auto"

    def test_ndjson_extension(self) -> None:
        assert SUPPORTED_READ_FUNCTIONS[".ndjson"] == "read_json_auto"

    def test_parquet_extension(self) -> None:
        assert SUPPORTED_READ_FUNCTIONS[".parquet"] == "read_parquet"

    def test_five_supported_extensions(self) -> None:
        assert len(SUPPORTED_READ_FUNCTIONS) == 5

    def test_all_keys_start_with_dot(self) -> None:
        for ext in SUPPORTED_READ_FUNCTIONS:
            assert ext.startswith(".")


@pytest.mark.unit
class TestGetReadFunction:
    """Tests for CSVLoader._get_read_function() format detection."""

    @pytest.fixture
    def loader(self, tmp_path: Path) -> CSVLoader:
        return CSVLoader(project_root=tmp_path, duckdb_path=tmp_path / "test.duckdb")

    def test_csv_returns_read_csv_auto(self, loader: CSVLoader) -> None:
        assert loader._get_read_function("data/sales.csv") == "read_csv_auto"

    def test_json_returns_read_json_auto(self, loader: CSVLoader) -> None:
        assert loader._get_read_function("data/events.json") == "read_json_auto"

    def test_jsonl_returns_read_json_auto(self, loader: CSVLoader) -> None:
        assert loader._get_read_function("data/logs.jsonl") == "read_json_auto"

    def test_ndjson_returns_read_json_auto(self, loader: CSVLoader) -> None:
        assert loader._get_read_function("data/stream.ndjson") == "read_json_auto"

    def test_parquet_returns_read_parquet(self, loader: CSVLoader) -> None:
        assert loader._get_read_function("data/warehouse.parquet") == "read_parquet"

    def test_uppercase_extension_normalized(self, loader: CSVLoader) -> None:
        assert loader._get_read_function("DATA/FILE.CSV") == "read_csv_auto"

    def test_mixed_case_extension(self, loader: CSVLoader) -> None:
        assert loader._get_read_function("file.Json") == "read_json_auto"

    def test_unsupported_xlsx_raises(self, loader: CSVLoader) -> None:
        with pytest.raises(ValueError, match="Unsupported file format"):
            loader._get_read_function("data/sheet.xlsx")

    def test_unsupported_txt_raises(self, loader: CSVLoader) -> None:
        with pytest.raises(ValueError, match="Unsupported file format"):
            loader._get_read_function("data/notes.txt")

    def test_unsupported_xml_raises(self, loader: CSVLoader) -> None:
        with pytest.raises(ValueError, match="Unsupported file format"):
            loader._get_read_function("data/feed.xml")

    def test_error_message_lists_supported(self, loader: CSVLoader) -> None:
        with pytest.raises(ValueError, match=r"\.csv"):
            loader._get_read_function("data/file.xlsx")

    def test_nested_path(self, loader: CSVLoader) -> None:
        assert loader._get_read_function("/a/b/c/d/file.parquet") == "read_parquet"


@pytest.mark.unit
class TestLoadFileFiltering:
    """Tests for file filtering by supported format in CSVLoader.load()."""

    @pytest.fixture
    def data_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "data"
        d.mkdir()
        return d

    def test_filters_unsupported_extensions(self, tmp_path: Path, data_dir: Path) -> None:
        """Unsupported files (xlsx, txt) are skipped; supported files are passed to _classify_files."""
        (data_dir / "good.csv").write_text("a,b\n1,2\n")
        (data_dir / "good.json").write_text('[{"a": 1}]')
        (data_dir / "bad.xlsx").write_bytes(b"fake")
        (data_dir / "bad.txt").write_text("hello")

        loader = CSVLoader(project_root=tmp_path, duckdb_path=tmp_path / "test.duckdb")
        config = CSVSourceConfig(directory=data_dir, file_pattern="*.*")

        with patch("dango.ingestion.csv_loader.duckdb") as mock_duckdb:
            mock_conn = MagicMock()
            mock_duckdb.connect.return_value = mock_conn
            with patch.object(
                loader,
                "_classify_files",
                return_value={
                    "new": [],
                    "updated": [],
                    "unchanged": [],
                    "deleted": [],
                },
            ) as mock_classify:
                with patch.object(loader, "_setup_metadata_table"):
                    with patch.object(loader, "_check_table_exists", return_value=False):
                        loader.load("test_source", config)

            # Only supported files passed to _classify_files
            call_args = mock_classify.call_args
            files_passed = call_args[0][2]  # third positional arg is current_files
            filenames = {Path(f).name for f in files_passed}
            assert filenames == {"good.csv", "good.json"}

    def test_accepts_local_files_source_config(self, tmp_path: Path, data_dir: Path) -> None:
        """LocalFilesSourceConfig is accepted by CSVLoader (inheritance)."""
        (data_dir / "data.parquet").write_bytes(b"PAR1fake")

        loader = CSVLoader(project_root=tmp_path, duckdb_path=tmp_path / "test.duckdb")
        config = LocalFilesSourceConfig(directory=data_dir)

        with patch("dango.ingestion.csv_loader.duckdb") as mock_duckdb:
            mock_conn = MagicMock()
            mock_duckdb.connect.return_value = mock_conn
            with patch.object(
                loader,
                "_classify_files",
                return_value={
                    "new": [],
                    "updated": [],
                    "unchanged": [],
                    "deleted": [],
                },
            ) as mock_classify:
                with patch.object(loader, "_setup_metadata_table"):
                    with patch.object(loader, "_check_table_exists", return_value=False):
                        result = loader.load("my_files", config)

            files_passed = mock_classify.call_args[0][2]
            assert len(files_passed) == 1
            assert files_passed[0].endswith("data.parquet")
            assert result["status"] == "success"

    def test_mixed_formats_all_supported(self, tmp_path: Path, data_dir: Path) -> None:
        """All supported formats in the same directory are included."""
        (data_dir / "a.csv").write_text("x\n1\n")
        (data_dir / "b.json").write_text("[]")
        (data_dir / "c.jsonl").write_text('{"x":1}\n')
        (data_dir / "d.parquet").write_bytes(b"PAR1fake")
        (data_dir / "e.ndjson").write_text('{"x":2}\n')

        loader = CSVLoader(project_root=tmp_path, duckdb_path=tmp_path / "test.duckdb")
        config = CSVSourceConfig(directory=data_dir, file_pattern="*.*")

        with patch("dango.ingestion.csv_loader.duckdb") as mock_duckdb:
            mock_conn = MagicMock()
            mock_duckdb.connect.return_value = mock_conn
            with patch.object(
                loader,
                "_classify_files",
                return_value={
                    "new": [],
                    "updated": [],
                    "unchanged": [],
                    "deleted": [],
                },
            ) as mock_classify:
                with patch.object(loader, "_setup_metadata_table"):
                    with patch.object(loader, "_check_table_exists", return_value=False):
                        loader.load("test_source", config)

            files_passed = mock_classify.call_args[0][2]
            assert len(files_passed) == 5


def _make_csv(directory: Path, filename: str, header: str, rows: list[str]) -> Path:
    """Write a CSV file with given header and rows."""
    filepath = directory / filename
    lines = [header] + rows
    filepath.write_text("\n".join(lines) + "\n")
    return filepath


@pytest.fixture()
def csv_env(tmp_path: Path) -> tuple[CSVLoader, Path, Path, CSVSourceConfig]:
    """Set up a CSV loader environment with DuckDB and a data directory.

    Mirrors the fixture in tests/unit/test_csv_schema_evolution.py — a real (not
    mocked) DuckDB file + real CSV files on disk, exercised through CSVLoader.load().
    A real connection is required here because the type-validation logic under test
    relies on genuine DuckDB type inference (read_csv_auto) and TRY_CAST semantics,
    which a mocked connection cannot meaningfully reproduce.
    """
    db_path = tmp_path / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True)

    data_dir = tmp_path / "csv_data"
    data_dir.mkdir()

    loader = CSVLoader(tmp_path, db_path)
    config = CSVSourceConfig(directory=data_dir, file_pattern="*.csv")

    return loader, db_path, data_dir, config


@pytest.mark.unit
class TestValidateSchemaMatchTypeChecking:
    """Tests for the column TYPE validation added to _validate_schema_match()."""

    def test_validate_schema_match_raises_on_type_mismatch(self, csv_env: Any) -> None:
        """A non-numeric value landing in a locked-in BIGINT column raises CSVSchemaMismatchError."""
        loader, _db_path, data_dir, config = csv_env

        # First file establishes the table with 'amount' inferred as BIGINT.
        _make_csv(data_dir, "file1.csv", "id,amount", ["1,100", "2,200"])
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"

        # Second file has a non-numeric value in 'amount' — cannot cast to BIGINT.
        _make_csv(data_dir, "file2.csv", "id,amount", ["3,not_a_number"])
        result = loader.load("test_src", config, "raw_test_src")

        assert result["status"] == "error"
        assert "amount" in result["error"]
        assert "not_a_number" in result["error"]
        assert "BIGINT" in result["error"]

    def test_validate_schema_match_allows_compatible_type_widening(self, csv_env: Any) -> None:
        """Numeric values loading into a table column locked as VARCHAR do not raise."""
        loader, _db_path, data_dir, config = csv_env

        # First file forces 'code' to infer as VARCHAR (contains a non-numeric value).
        _make_csv(data_dir, "file1.csv", "id,code", ["1,ABC123"])
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"

        # Second file's 'code' column is purely numeric -> infers as BIGINT for this
        # file, but every value casts safely into the table's locked-in VARCHAR type.
        _make_csv(data_dir, "file2.csv", "id,code", ["2,456"])
        result = loader.load("test_src", config, "raw_test_src")

        assert result["status"] == "success"
        assert result["total_rows"] == 2

    def test_validate_schema_match_allows_null_values_in_differing_type_column(
        self, csv_env: Any
    ) -> None:
        """A differing-type column that is entirely NULL/blank in the new file does not raise."""
        loader, _db_path, data_dir, config = csv_env

        # First file establishes 'amount' as BIGINT.
        _make_csv(data_dir, "file1.csv", "id,amount", ["1,100", "2,200"])
        result = loader.load("test_src", config, "raw_test_src")
        assert result["status"] == "success"

        # Second file's 'amount' column is blank on every row -> DuckDB infers
        # VARCHAR for this file (a genuine type difference from the table's BIGINT),
        # but since every value is NULL the IS NOT NULL guard must prevent a raise.
        _make_csv(data_dir, "file2.csv", "id,amount", ["3,", "4,"])
        result = loader.load("test_src", config, "raw_test_src")

        assert result["status"] == "success"

    def test_validate_schema_match_unchanged_for_matching_types(self, tmp_path: Path) -> None:
        """When file and table column types match exactly, no extra TRY_CAST query runs."""
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True)
        data_dir = tmp_path / "csv_data"
        data_dir.mkdir()

        loader = CSVLoader(tmp_path, db_path)
        target_table = "raw_test_src.test_src"

        file1 = _make_csv(data_dir, "file1.csv", "id,amount", ["1,100", "2,200"])
        file2 = _make_csv(data_dir, "file2.csv", "id,amount", ["3,300"])

        conn = duckdb.connect(str(db_path))
        try:
            conn.execute("CREATE SCHEMA IF NOT EXISTS raw_test_src")
            loader._create_table_from_file(conn, str(file1), target_table, "test_src")

            # Spy on the real connection (wraps=conn) so real SQL executes and
            # real results come back, while call_args_list records every query.
            spy_conn = MagicMock(wraps=conn)
            loader._validate_schema_match(spy_conn, str(file2), target_table, "test_src")

            try_cast_calls = [
                call for call in spy_conn.execute.call_args_list if "TRY_CAST" in call.args[0]
            ]
            assert try_cast_calls == []
        finally:
            conn.close()
