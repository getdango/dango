"""tests/unit/test_csv_loader.py

Tests for dango.ingestion.csv_loader — multi-format file loading.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

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
