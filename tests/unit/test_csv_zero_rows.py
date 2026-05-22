"""tests/unit/test_csv_zero_rows.py

Unit tests for CSV 0-row sync detection in dlt_runner.py.

When CSVLoader returns success with 0 rows, 0 files processed, and 0 deletions,
dlt_runner should convert to error status (first sync with no files is an error).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dango.ingestion.dlt_runner import DltPipelineRunner


def _make_runner(tmp_path):
    """Create a DltPipelineRunner without calling __init__."""
    runner = DltPipelineRunner.__new__(DltPipelineRunner)
    runner.project_root = tmp_path
    runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
    runner.allow_schema_changes = False
    return runner


def _make_source_config(name="test_csv", source_type="csv"):
    """Create a minimal mock source config."""
    cfg = MagicMock()
    cfg.name = name
    cfg.source_type = source_type
    cfg.csv = MagicMock() if source_type == "csv" else None
    cfg.local_files = MagicMock() if source_type == "local_files" else None
    return cfg


@pytest.mark.unit
class TestCsvSourceZeroRows:
    """Tests for _run_csv_source 0-row detection."""

    @patch("dango.ingestion.dlt_runner.CSVLoader")
    def test_first_sync_no_files_is_error(self, mock_csv_cls, tmp_path):
        """CSVLoader returns success with 0 rows/files → error."""
        mock_loader = MagicMock()
        mock_loader.load.return_value = {
            "status": "success",
            "total_rows": 0,
            "new": 0,
            "updated": 0,
            "deleted": 0,
        }
        mock_csv_cls.return_value = mock_loader

        runner = _make_runner(tmp_path)
        cfg = _make_source_config("test_csv", "csv")
        result = runner._run_csv_source(cfg)

        assert result["status"] == "error"
        assert "No files found" in result["error"]

    @patch("dango.ingestion.dlt_runner.CSVLoader")
    def test_with_rows_stays_success(self, mock_csv_cls, tmp_path):
        """CSVLoader returns success with rows → stays success."""
        mock_loader = MagicMock()
        mock_loader.load.return_value = {
            "status": "success",
            "total_rows": 100,
            "new": 1,
            "updated": 0,
            "deleted": 0,
        }
        mock_csv_cls.return_value = mock_loader

        runner = _make_runner(tmp_path)
        cfg = _make_source_config("test_csv", "csv")
        result = runner._run_csv_source(cfg)

        assert result["status"] == "success"
        assert result["rows_loaded"] == 100

    @patch("dango.ingestion.dlt_runner.CSVLoader")
    def test_deletion_sync_stays_success(self, mock_csv_cls, tmp_path):
        """CSVLoader returns 0 rows but deletions processed → stays success."""
        mock_loader = MagicMock()
        mock_loader.load.return_value = {
            "status": "success",
            "total_rows": 0,
            "new": 0,
            "updated": 0,
            "deleted": 2,
        }
        mock_csv_cls.return_value = mock_loader

        runner = _make_runner(tmp_path)
        cfg = _make_source_config("test_csv", "csv")
        result = runner._run_csv_source(cfg)

        assert result["status"] == "success"

    @patch("dango.ingestion.dlt_runner.CSVLoader")
    def test_existing_error_not_overwritten(self, mock_csv_cls, tmp_path):
        """CSVLoader returning error with 0 rows keeps original error."""
        mock_loader = MagicMock()
        mock_loader.load.return_value = {
            "status": "error",
            "total_rows": 0,
            "new": 0,
            "updated": 0,
            "deleted": 0,
            "error": "Parse failed: invalid CSV",
        }
        mock_csv_cls.return_value = mock_loader

        runner = _make_runner(tmp_path)
        cfg = _make_source_config("test_csv", "csv")
        result = runner._run_csv_source(cfg)

        assert result["status"] == "error"
        assert result["error"] == "Parse failed: invalid CSV"


@pytest.mark.unit
class TestLocalFilesSourceZeroRows:
    """Tests for _run_local_files_source 0-row detection."""

    @patch("dango.ingestion.dlt_runner.CSVLoader")
    def test_no_files_is_error(self, mock_csv_cls, tmp_path):
        """Local files source with 0 rows/files → error."""
        mock_loader = MagicMock()
        mock_loader.load.return_value = {
            "status": "success",
            "total_rows": 0,
            "new": 0,
            "updated": 0,
            "deleted": 0,
        }
        mock_csv_cls.return_value = mock_loader

        runner = _make_runner(tmp_path)
        cfg = _make_source_config("test_local", "local_files")
        result = runner._run_local_files_source(cfg)

        assert result["status"] == "error"
        assert "No files found" in result["error"]
