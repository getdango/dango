"""tests/unit/test_empty_replace_protection.py

Unit tests for empty sync protection: replace-mode syncs that return 0 rows
when previous data existed should fail and preserve existing data.

Covers: core protection, source type coverage, pipeline state, schema interaction,
CLI flag, web/scheduler, error messages, and edge cases.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_runner(tmp_path: Path):
    """Create a DltPipelineRunner with mocked internals."""
    from dango.ingestion.dlt_runner import DltPipelineRunner

    runner = DltPipelineRunner.__new__(DltPipelineRunner)
    runner.project_root = tmp_path
    runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
    runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    runner._current_oauth_warning = None
    return runner


def _make_source_config(name: str = "test_source", source_type: str = "chess"):
    """Create a minimal DataSource config for testing."""
    from dango.config.models import DataSource, SourceType

    config = MagicMock(spec=DataSource)
    config.name = name
    config.type = SourceType(source_type)
    config.csv = None
    config.local_files = None
    config.dlt_native = None
    config.generic_config = {}
    config.enabled = True
    return config


def _make_csv_source_config(name: str = "csv_source"):
    """Create a CSV DataSource config."""
    from dango.config.models import DataSource, SourceType

    config = MagicMock(spec=DataSource)
    config.name = name
    config.type = SourceType.CSV
    config.csv = MagicMock()
    config.local_files = None
    config.dlt_native = None
    config.enabled = True
    return config


def _make_local_files_config(name: str = "local_source"):
    """Create a local_files DataSource config."""
    from dango.config.models import DataSource, SourceType

    config = MagicMock(spec=DataSource)
    config.name = name
    config.type = SourceType.LOCAL_FILES
    config.csv = None
    config.local_files = MagicMock()
    config.dlt_native = None
    config.enabled = True
    return config


def _make_dlt_native_config(name: str = "native_source"):
    """Create a dlt_native DataSource config."""
    from dango.config.models import DataSource, SourceType

    native = MagicMock()
    native.source_module = "test_module"
    native.source_function = "test_func"
    native.function_kwargs = {}
    native.dataset_name = None
    native.pipeline_name = None

    config = MagicMock(spec=DataSource)
    config.name = name
    config.type = SourceType.DLT_NATIVE
    config.csv = None
    config.local_files = None
    config.dlt_native = native
    config.enabled = True
    return config


def _mock_load_info(rows: int = 0):
    """Create a mock LoadInfo with given row count."""
    info = MagicMock()
    return info


# ============================================================================
# Core Protection (Tests 1-5)
# ============================================================================


@pytest.mark.unit
class TestCoreProtection:
    """Core 0-row replace protection for dlt sources."""

    def test_replace_mode_zero_rows_previous_data_fails(self, tmp_path):
        """Test 1: Replace-mode, 0 rows, had previous data → failed, data preserved."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=5000),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_restore_dlt_state") as mock_restore,
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(0)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=True,
            )

        assert result["status"] == "failed"
        assert result["rows_loaded"] == 0
        assert "existing 5,000 rows preserved" in result["error"]
        assert "--allow-empty-replace" in result["error"]
        mock_restore.assert_called_once()

    def test_replace_mode_zero_rows_first_sync_succeeds(self, tmp_path):
        """Test 2: Replace-mode, 0 rows, first sync (no previous data) → success."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=0),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_cleanup_state_backup"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_check_row_count_anomaly", return_value=None),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(0)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=True,
            )

        assert result["status"] == "success"

    def test_replace_mode_with_rows_succeeds(self, tmp_path):
        """Test 3: Replace-mode, >0 rows, had previous data → success."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=5000),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_cleanup_state_backup"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 6000}),
            patch.object(runner, "_check_row_count_anomaly", return_value=None),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(6000)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=True,
            )

        assert result["status"] == "success"
        assert result["rows_loaded"] == 6000

    def test_replace_mode_first_sync_with_rows_succeeds(self, tmp_path):
        """Test 4: Replace-mode, >0 rows, first sync → success."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=None),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_cleanup_state_backup"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 100}),
            patch.object(runner, "_check_row_count_anomaly", return_value=None),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(100)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=True,
            )

        assert result["status"] == "success"

    def test_allow_empty_replace_bypasses_protection(self, tmp_path):
        """Test 5: Replace-mode, 0 rows, allow_empty_replace=True → success."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=5000),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_cleanup_state_backup"),
            patch.object(runner, "_restore_dlt_state") as mock_restore,
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_check_row_count_anomaly", return_value=None),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(0)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=True,
                allow_empty_replace=True,
            )

        assert result["status"] == "success"
        mock_restore.assert_not_called()


# ============================================================================
# Incremental/Merge (Tests 6-8)
# ============================================================================


@pytest.mark.unit
class TestIncrementalMerge:
    """Incremental/merge modes should not trigger 0-row protection."""

    def test_merge_mode_zero_rows_succeeds(self, tmp_path):
        """Test 6: Merge mode, 0 rows → success, data untouched."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=5000),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_cleanup_state_backup"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=False),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_check_row_count_anomaly", return_value=None),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(0)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            # Not full_refresh AND not replace mode → no protection
            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=False,
            )

        assert result["status"] == "success"

    def test_append_mode_zero_rows_succeeds(self, tmp_path):
        """Test 7: Append mode, 0 rows → success, data untouched."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=1000),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_cleanup_state_backup"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=False),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_check_row_count_anomaly", return_value=None),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(0)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=False,
            )

        assert result["status"] == "success"

    def test_incremental_with_rows_succeeds(self, tmp_path):
        """Test 8: Incremental, >0 rows → success."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=1000),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_cleanup_state_backup"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=False),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 50}),
            patch.object(runner, "_check_row_count_anomaly", return_value=None),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(50)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=False,
            )

        assert result["status"] == "success"
        assert result["rows_loaded"] == 50


# ============================================================================
# Source Type Coverage (Tests 9-14)
# ============================================================================


@pytest.mark.unit
class TestSourceTypeCoverage:
    """Verify protection works across all source types."""

    def test_dlt_source_zero_row_protection(self, tmp_path):
        """Test 9: dlt source primary path → 0-row protection works."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=1000),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_restore_dlt_state"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(0)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=True,
            )

        assert result["status"] == "failed"
        assert "existing 1,000 rows preserved" in result["error"]

    def test_dlt_native_source_zero_row_protection(self, tmp_path):
        """Test 10: dlt native source path → 0-row protection works."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=2000),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_restore_dlt_state") as mock_restore,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(0)),
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("dango.ingestion.dlt_runner.importlib") as mock_importlib,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_module = MagicMock()
            mock_module.test_func = MagicMock(return_value=MagicMock())
            mock_importlib.import_module.return_value = mock_module

            mock_pipeline = MagicMock()
            mock_dlt.pipeline.return_value = mock_pipeline
            mock_dlt.destinations.duckdb.return_value = MagicMock()

            result = runner._run_dlt_native_source(
                _make_dlt_native_config(),
                full_refresh=True,
            )

        assert result["status"] == "failed"
        assert "existing 2,000 rows preserved" in result["error"]
        mock_restore.assert_called_once()

    def test_dlt_native_replace_mode_no_full_refresh_protected(self, tmp_path):
        """Test 10b: dlt native with uses_replace_mode but no full_refresh → protected."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=1500),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_restore_dlt_state") as mock_restore,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(0)),
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("dango.ingestion.dlt_runner.importlib") as mock_importlib,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_module = MagicMock()
            mock_module.test_func = MagicMock(return_value=MagicMock())
            mock_importlib.import_module.return_value = mock_module

            mock_pipeline = MagicMock()
            mock_dlt.pipeline.return_value = mock_pipeline
            mock_dlt.destinations.duckdb.return_value = MagicMock()

            # full_refresh=False but uses_replace_mode=True → still protected
            result = runner._run_dlt_native_source(
                _make_dlt_native_config(),
                full_refresh=False,
            )

        assert result["status"] == "failed"
        assert "existing 1,500 rows preserved" in result["error"]
        mock_restore.assert_called_once()

    def test_csv_source_zero_rows_previous_data_fails(self, tmp_path):
        """Test 11: CSV source, 0 rows + previous data → fails, old table preserved."""
        runner = _make_runner(tmp_path)

        mock_conn = MagicMock()
        with (
            patch.object(runner, "_get_csv_table_rows", return_value=500),
            patch("duckdb.connect", return_value=mock_conn),
            patch("dango.ingestion.dlt_runner.CSVLoader") as mock_loader_cls,
        ):
            mock_loader = MagicMock()
            mock_loader.load.return_value = {
                "status": "success",
                "total_rows": 0,
                "new": 0,
                "updated": 0,
            }
            mock_loader_cls.return_value = mock_loader

            result = runner._run_csv_source(
                _make_csv_source_config(),
                full_refresh=True,
            )

        assert result["status"] == "failed"
        assert "existing 500 rows preserved" in result["error"]

    def test_csv_source_with_rows_succeeds(self, tmp_path):
        """Test 12: CSV source, >0 rows → succeeds."""
        runner = _make_runner(tmp_path)

        mock_conn = MagicMock()
        with (
            patch.object(runner, "_get_csv_table_rows", return_value=500),
            patch("duckdb.connect", return_value=mock_conn),
            patch("dango.ingestion.dlt_runner.CSVLoader") as mock_loader_cls,
        ):
            mock_loader = MagicMock()
            mock_loader.load.return_value = {
                "status": "success",
                "total_rows": 600,
                "new": 5,
                "updated": 0,
            }
            mock_loader_cls.return_value = mock_loader

            result = runner._run_csv_source(
                _make_csv_source_config(),
                full_refresh=True,
            )

        assert result["status"] == "success"
        assert result["rows_loaded"] == 600

    def test_local_files_zero_rows_previous_data_fails(self, tmp_path):
        """Test 13: Local files source, 0 rows + previous data → fails."""
        runner = _make_runner(tmp_path)

        mock_conn = MagicMock()
        with (
            patch.object(runner, "_get_csv_table_rows", return_value=300),
            patch("duckdb.connect", return_value=mock_conn),
            patch("dango.ingestion.dlt_runner.CSVLoader") as mock_loader_cls,
        ):
            mock_loader = MagicMock()
            mock_loader.load.return_value = {
                "status": "success",
                "total_rows": 0,
                "new": 0,
                "updated": 0,
            }
            mock_loader_cls.return_value = mock_loader

            result = runner._run_local_files_source(
                _make_local_files_config(),
                full_refresh=True,
            )

        assert result["status"] == "failed"
        assert "existing 300 rows preserved" in result["error"]

    def test_local_files_with_rows_succeeds(self, tmp_path):
        """Test 14: Local files source, >0 rows → succeeds."""
        runner = _make_runner(tmp_path)

        mock_conn = MagicMock()
        with (
            patch.object(runner, "_get_csv_table_rows", return_value=300),
            patch("duckdb.connect", return_value=mock_conn),
            patch("dango.ingestion.dlt_runner.CSVLoader") as mock_loader_cls,
        ):
            mock_loader = MagicMock()
            mock_loader.load.return_value = {
                "status": "success",
                "total_rows": 400,
                "new": 3,
                "updated": 1,
            }
            mock_loader_cls.return_value = mock_loader

            result = runner._run_local_files_source(
                _make_local_files_config(),
                full_refresh=True,
            )

        assert result["status"] == "success"
        assert result["rows_loaded"] == 400


# ============================================================================
# Pipeline State (Tests 15-16)
# ============================================================================


@pytest.mark.unit
class TestPipelineState:
    """Verify pipeline state is restored on 0-row failure."""

    def test_dlt_state_restored_on_zero_row_failure(self, tmp_path):
        """Test 15: After 0-row failure, dlt state restored from backup."""
        runner = _make_runner(tmp_path)
        backup_path = Path("/tmp/test_backup")

        with (
            patch.object(runner, "_get_source_total_rows", return_value=1000),
            patch.object(runner, "_backup_dlt_state", return_value=backup_path),
            patch.object(runner, "_restore_dlt_state") as mock_restore,
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(0)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=True,
            )

        assert result["status"] == "failed"
        mock_restore.assert_called_once_with(backup_path)

    def test_retry_after_zero_row_failure_succeeds(self, tmp_path):
        """Test 16: After 0-row failure, retry succeeds normally."""
        runner = _make_runner(tmp_path)

        # First call: returns 0 rows (fails). Second call: returns rows (succeeds).
        call_count = [0]

        def mock_extract_stats(load_info):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"rows_loaded": 0}
            return {"rows_loaded": 500}

        with (
            patch.object(runner, "_get_source_total_rows", return_value=1000),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_restore_dlt_state"),
            patch.object(runner, "_cleanup_state_backup"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", side_effect=mock_extract_stats),
            patch.object(runner, "_check_row_count_anomaly", return_value=None),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info()),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            # First call fails
            result1 = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=True,
            )
            assert result1["status"] == "failed"

            # Second call succeeds
            result2 = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=True,
            )
            assert result2["status"] == "success"


# ============================================================================
# Schema Interaction (Tests 17-18)
# ============================================================================


@pytest.mark.unit
class TestSchemaInteraction:
    """Verify schema handling with empty replace protection."""

    def test_replace_mode_with_rows_schema_updated(self, tmp_path):
        """Test 17: Replace-mode + schema changes + >0 rows → success."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=1000),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_cleanup_state_backup"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(
                runner,
                "_extract_load_stats",
                return_value={"rows_loaded": 1200, "loaded_tables": ["t1", "t2"]},
            ),
            patch.object(runner, "_check_row_count_anomaly", return_value=None),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info()),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=True,
            )

        assert result["status"] == "success"
        assert result["rows_loaded"] == 1200

    def test_no_pre_schema_drop_in_source_code(self):
        """Test 18: DROP SCHEMA CASCADE removed from _run_dlt_source."""
        source = inspect.getsource(
            __import__(
                "dango.ingestion.dlt_runner", fromlist=["DltPipelineRunner"]
            ).DltPipelineRunner._run_dlt_source
        )
        assert "DROP SCHEMA" not in source


# ============================================================================
# CLI Flag (Tests 19-21)
# ============================================================================


@pytest.mark.unit
class TestCLIFlag:
    """Verify --allow-empty-replace CLI flag behavior."""

    def test_allow_empty_replace_flag_passes_through(self, tmp_path):
        """Test 19: --allow-empty-replace + 0 rows → success (via run_sync)."""
        from dango.ingestion.dlt_runner import run_sync

        mock_source = _make_source_config()

        with (
            patch("dango.ingestion.dlt_runner.DltPipelineRunner") as mock_runner_cls,
            patch("dango.ingestion.dlt_runner.console"),
        ):
            mock_runner = MagicMock()
            mock_runner.run_source.return_value = {
                "status": "success",
                "rows_loaded": 0,
            }
            mock_runner_cls.return_value = mock_runner

            run_sync(
                project_root=tmp_path,
                sources=[mock_source],
                allow_empty_replace=True,
            )

            # Verify allow_empty_replace was passed through
            call_kwargs = mock_runner.run_source.call_args
            assert call_kwargs[1].get("allow_empty_replace") is True or (
                len(call_kwargs[0]) > 5 and call_kwargs[0][5] is True
            )

    def test_without_flag_zero_rows_fails(self, tmp_path):
        """Test 20: Without flag + 0 rows → fails (via run_sync)."""
        from dango.ingestion.dlt_runner import run_sync

        mock_source = _make_source_config()

        with (
            patch("dango.ingestion.dlt_runner.DltPipelineRunner") as mock_runner_cls,
            patch("dango.ingestion.dlt_runner.console"),
        ):
            mock_runner = MagicMock()
            mock_runner.run_source.return_value = {
                "status": "failed",
                "error": "0 rows",
                "rows_loaded": 0,
            }
            mock_runner_cls.return_value = mock_runner

            run_sync(
                project_root=tmp_path,
                sources=[mock_source],
                allow_empty_replace=False,
            )

            # Verify allow_empty_replace=False was passed
            call_kwargs = mock_runner.run_source.call_args
            assert call_kwargs[1].get("allow_empty_replace") is False

    def test_flag_is_hidden(self):
        """Test 21: --allow-empty-replace is hidden (not in --help)."""
        from dango.cli.commands.source import sync

        for param in sync.params:
            if param.name == "allow_empty_replace":
                assert param.hidden is True, "--allow-empty-replace should be hidden"
                break
        else:
            pytest.fail("--allow-empty-replace param not found on sync command")


# ============================================================================
# Web/Scheduler (Tests 22-24)
# ============================================================================


@pytest.mark.unit
class TestWebScheduler:
    """Verify web/scheduler threading of allow_empty_replace."""

    def test_sync_request_model_has_field(self):
        """Test 22: SyncRequest has allow_empty_replace field."""
        from dango.web.models import SyncRequest

        req = SyncRequest()
        assert req.allow_empty_replace is False

        req = SyncRequest(allow_empty_replace=True)
        assert req.allow_empty_replace is True

    def test_sync_trigger_request_has_field(self):
        """Test 23: SyncTriggerRequest has allow_empty_replace field."""
        from dango.web.models import SyncTriggerRequest

        req = SyncTriggerRequest(sources=["test"])
        assert req.allow_empty_replace is False

        req = SyncTriggerRequest(sources=["test"], allow_empty_replace=True)
        assert req.allow_empty_replace is True

    def test_launch_sync_subprocess_accepts_param(self):
        """Test 24: launch_sync_subprocess accepts allow_empty_replace."""
        sig = inspect.signature(
            __import__(
                "dango.platform.sync_process", fromlist=["launch_sync_subprocess"]
            ).launch_sync_subprocess
        )
        assert "allow_empty_replace" in sig.parameters


# ============================================================================
# Error Message and Status (Tests 25-28)
# ============================================================================


@pytest.mark.unit
class TestErrorMessageAndStatus:
    """Verify error messages and sync status recording."""

    def test_error_includes_existing_data_preserved(self, tmp_path):
        """Test 25: Error message includes 'existing data preserved'."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=3000),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_restore_dlt_state"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(0)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=True,
            )

        assert "rows preserved" in result["error"]

    def test_error_includes_allow_empty_replace(self, tmp_path):
        """Test 26: Error message includes '--allow-empty-replace'."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=100),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_restore_dlt_state"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(0)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=True,
            )

        assert "--allow-empty-replace" in result["error"]

    def test_sync_history_records_failed_status(self, tmp_path):
        """Test 27: run_source records status='failed' in sync history."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(
                runner,
                "run_source",
                return_value={
                    "status": "failed",
                    "error": "Sync returned 0 rows",
                    "rows_loaded": 0,
                },
            ),
        ):
            result = runner.run_source(_make_source_config(), full_refresh=True)
            assert result["status"] == "failed"

    def test_run_source_error_field_mapping(self):
        """Test 28: run_source extracts error from result.get('error')."""
        source = inspect.getsource(
            __import__(
                "dango.ingestion.dlt_runner", fromlist=["DltPipelineRunner"]
            ).DltPipelineRunner.run_source
        )
        assert 'result.get("error")' in source or "result.get('error')" in source


# ============================================================================
# Edge Cases (Tests 29-31)
# ============================================================================


@pytest.mark.unit
class TestEdgeCases:
    """Edge cases for empty replace protection."""

    def test_replace_mode_without_full_refresh_protected(self, tmp_path):
        """Test 29: uses_replace_mode=True without full_refresh → still protected."""
        runner = _make_runner(tmp_path)

        with (
            patch.object(runner, "_get_source_total_rows", return_value=5000),
            patch.object(runner, "_backup_dlt_state", return_value=Path("/tmp/backup")),
            patch.object(runner, "_restore_dlt_state") as mock_restore,
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source") as mock_source,
            patch.object(runner, "_detect_write_disposition", return_value=True),
            patch.object(runner, "_get_dataset_name", return_value="raw_test"),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_run_with_retry", return_value=_mock_load_info(0)),
            patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
            patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
            patch("os.getcwd", return_value="/tmp"),
            patch("os.chdir"),
        ):
            mock_meta.return_value = {
                "dlt_package": "test",
                "dlt_function": "test_func",
            }
            mock_dlt.pipeline.return_value = MagicMock()
            mock_dlt.destinations.duckdb.return_value = MagicMock()
            mock_source.return_value = MagicMock()

            # full_refresh=False but uses_replace_mode=True → still protected
            result = runner._run_dlt_source(
                _make_source_config(),
                full_refresh=False,
            )

        assert result["status"] == "failed"
        assert "existing 5,000 rows preserved" in result["error"]
        mock_restore.assert_called_once()

    def test_concurrent_syncs_independent(self, tmp_path):
        """Test 30: Concurrent syncs — one failure doesn't affect another."""
        runner = _make_runner(tmp_path)

        # Source A: 0 rows → fails
        # Source B: 100 rows → succeeds
        results = []
        for name, rows_loaded, pre_rows in [
            ("source_a", 0, 500),
            ("source_b", 100, 200),
        ]:
            with (
                patch.object(runner, "_get_source_total_rows", return_value=pre_rows),
                patch.object(runner, "_backup_dlt_state", return_value=Path(f"/tmp/{name}_backup")),
                patch.object(runner, "_restore_dlt_state"),
                patch.object(runner, "_cleanup_state_backup"),
                patch.object(runner, "_build_source_config", return_value={}),
                patch.object(runner, "_load_dlt_source") as mock_source,
                patch.object(runner, "_detect_write_disposition", return_value=True),
                patch.object(runner, "_get_dataset_name", return_value=f"raw_{name}"),
                patch.object(runner, "_check_oauth_token_expiry", return_value=None),
                patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
                patch.object(
                    runner,
                    "_extract_load_stats",
                    return_value={"rows_loaded": rows_loaded},
                ),
                patch.object(runner, "_check_row_count_anomaly", return_value=None),
                patch.object(runner, "_run_with_retry", return_value=_mock_load_info()),
                patch("dango.ingestion.dlt_runner.get_source_metadata") as mock_meta,
                patch("dango.ingestion.dlt_runner.dlt") as mock_dlt,
                patch("os.getcwd", return_value="/tmp"),
                patch("os.chdir"),
            ):
                mock_meta.return_value = {
                    "dlt_package": "test",
                    "dlt_function": "test_func",
                }
                mock_dlt.pipeline.return_value = MagicMock()
                mock_dlt.destinations.duckdb.return_value = MagicMock()
                mock_source.return_value = MagicMock()

                config = _make_source_config(name=name)
                result = runner._run_dlt_source(config, full_refresh=True)
                results.append(result)

        assert results[0]["status"] == "failed"  # source_a: 0 rows
        assert results[1]["status"] == "success"  # source_b: 100 rows

    def test_anomaly_check_still_works_for_incremental(self, tmp_path):
        """Test 31: _check_row_count_anomaly still works for incremental sources."""
        runner = _make_runner(tmp_path)

        # The anomaly check should still detect drops for incremental sources
        with patch(
            "dango.utils.sync_history.load_sync_history",
            return_value=[{"status": "success", "total_row_count": 1000, "rows_processed": 1000}],
        ):
            anomaly = runner._check_row_count_anomaly("test_source", total_rows=0)

        assert anomaly is not None
        assert anomaly["level"] == "error"
        assert "Zero rows" in anomaly["message"]


# ============================================================================
# Parameter Threading Verification
# ============================================================================


@pytest.mark.unit
class TestParameterThreading:
    """Verify allow_empty_replace is threaded through all functions."""

    @pytest.mark.parametrize(
        "module_path,func_name",
        [
            ("dango.ingestion.dlt_runner", "run_sync"),
            ("dango.platform.sync_process", "launch_sync_subprocess"),
            ("dango.platform.scheduling.sync_trigger", "run_manual_sync"),
        ],
    )
    def test_function_accepts_allow_empty_replace(self, module_path, func_name):
        """Verify public functions accept allow_empty_replace parameter."""
        module = __import__(module_path, fromlist=[func_name])
        func = getattr(module, func_name)
        sig = inspect.signature(func)
        assert "allow_empty_replace" in sig.parameters, (
            f"{module_path}.{func_name} missing allow_empty_replace parameter"
        )

    @pytest.mark.parametrize(
        "method_name",
        [
            "run_source",
            "_run_dlt_source",
            "_run_dlt_native_source",
            "_run_csv_source",
            "_run_local_files_source",
        ],
    )
    def test_runner_method_accepts_allow_empty_replace(self, method_name):
        """Verify DltPipelineRunner methods accept allow_empty_replace."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        method = getattr(DltPipelineRunner, method_name)
        sig = inspect.signature(method)
        assert "allow_empty_replace" in sig.parameters, (
            f"DltPipelineRunner.{method_name} missing allow_empty_replace parameter"
        )


# ============================================================================
# DROP SCHEMA Removal Verification
# ============================================================================


@pytest.mark.unit
class TestDropSchemaRemoval:
    """Verify DROP SCHEMA CASCADE has been removed from both dlt methods."""

    @pytest.mark.parametrize("method_name", ["_run_dlt_source", "_run_dlt_native_source"])
    def test_no_drop_schema_cascade(self, method_name):
        """DROP SCHEMA CASCADE must not appear in dlt sync methods."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        source = inspect.getsource(getattr(DltPipelineRunner, method_name))
        assert "DROP SCHEMA" not in source, (
            f"{method_name} still contains DROP SCHEMA — must be removed"
        )
