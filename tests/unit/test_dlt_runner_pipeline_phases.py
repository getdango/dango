"""tests/unit/test_dlt_runner_pipeline_phases.py

Tests for the split-phase pipeline: extract (no lock) → normalize (no lock) →
load (locked). BUG-S3-2.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import dlt
import pytest

import dango.utils.dbt_lock  # noqa: F401 — force submodule into sys.modules

# dango.utils.__init__ exports a *function* named dbt_lock which shadows the
# submodule dango.utils.dbt_lock.  Fetch the module from sys.modules to get
# the real module for patch.object().
_dbt_lock_module = sys.modules["dango.utils.dbt_lock"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_pipeline():
    """Create a minimal dlt.Pipeline mock with extract/normalize/load methods."""
    p = MagicMock(spec=dlt.Pipeline)
    p.extract.return_value = None
    p.normalize.return_value = None
    p.dataset_name = "raw_test_source"
    load_info = MagicMock()
    load_info.load_id = "test-load-id"
    load_info.metrics = {}
    load_info.dataset_name = "raw_test_source"
    p.load.return_value = load_info
    return p


def _make_source_config(name="test_source", source_type_value="hubspot"):
    """Create a minimal DataSource mock without strict spec.

    Uses plain MagicMock (no spec) so nested attributes like ``type.value``
    can be set freely. The real DataSource is a Pydantic model whose
    attribute tree doesn't map well onto MagicMock's spec.
    """
    src = MagicMock()
    src.name = name
    src.type = MagicMock()
    src.type.value = source_type_value
    src.enabled = True
    src.csv = None
    src.dlt_native = None
    return src


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPipelinePhases:
    """Tests for the extract/normalize/load split in _run_dlt_source()."""

    @patch("dango.ingestion.dlt_runner.get_source_metadata")
    @patch("dlt.pipeline")
    @patch("dango.ingestion.dlt_runner.os.chdir")
    def test_extract_normalize_no_lock(
        self,
        mock_chdir,
        mock_dlt_pipeline_cls,
        mock_get_meta,
        tmp_path,
    ):
        """DbtLock should NOT be called during extract or normalize phases."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        mock_get_meta.return_value = {
            "dlt_package": "dlt.sources.test",
            "dlt_function": "test_source",
        }
        runner = DltPipelineRunner(tmp_path)
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)

        source_config = _make_source_config()
        pipeline = _make_mock_pipeline()
        mock_dlt_pipeline_cls.return_value = pipeline

        with (
            patch.object(_dbt_lock_module, "DbtLock") as mock_lock_cls,
            patch.object(runner, "_load_dlt_source", return_value=MagicMock()),
            patch.object(runner, "_backup_dlt_state", return_value=None),
            patch.object(runner, "_get_source_total_rows", return_value=100),
            patch.object(runner, "_get_source_table_rows", return_value={}),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 50}),
            patch.object(runner, "_detect_write_disposition", return_value=False),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", return_value={}),
            patch.object(runner, "_get_dataset_name", return_value="raw_test_source"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_cleanup_state_backup"),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            runner._run_dlt_source(source_config)

        # Extract should have been called
        pipeline.extract.assert_called_once()
        # Normalize should have been called
        pipeline.normalize.assert_called_once()
        # Load should have been called
        pipeline.load.assert_called_once()
        # Lock was NOT acquired during extract or normalize
        mock_lock_cls.return_value.acquire.assert_called_once()
        # Lock was released after load
        mock_lock_cls.return_value.release.assert_called_once()

    @patch("dango.ingestion.dlt_runner.get_source_metadata")
    @patch("dlt.pipeline")
    @patch("dango.ingestion.dlt_runner.os.chdir")
    def test_load_acquires_lock(
        self,
        mock_chdir,
        mock_dlt_pipeline_cls,
        mock_get_meta,
        tmp_path,
    ):
        """DbtLock.acquire() must be called before pipeline.load()."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        mock_get_meta.return_value = {
            "dlt_package": "dlt.sources.test",
            "dlt_function": "test_source",
        }
        runner = DltPipelineRunner(tmp_path)
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)

        source_config = _make_source_config()
        pipeline = _make_mock_pipeline()
        mock_dlt_pipeline_cls.return_value = pipeline

        call_order = []

        def _track_acquire(*args, **kwargs):
            call_order.append("acquire")
            return True

        def _track_load(*args, **kwargs):
            call_order.append("load")
            return pipeline.load.return_value

        def _track_release(*args, **kwargs):
            call_order.append("release")

        with (
            patch.object(_dbt_lock_module, "DbtLock") as mock_lock_cls,
            patch.object(runner, "_load_dlt_source", return_value=MagicMock()),
            patch.object(runner, "_backup_dlt_state", return_value=None),
            patch.object(runner, "_get_source_total_rows", return_value=100),
            patch.object(runner, "_get_source_table_rows", return_value={}),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 50}),
            patch.object(runner, "_detect_write_disposition", return_value=False),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", return_value={}),
            patch.object(runner, "_get_dataset_name", return_value="raw_test_source"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_cleanup_state_backup"),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            mock_lock_cls.return_value.acquire.side_effect = _track_acquire
            mock_lock_cls.return_value.release.side_effect = _track_release
            pipeline.load.side_effect = _track_load
            runner._run_dlt_source(source_config)

        # Extract happens first, then normalize, then acquire, then load, then release
        assert "acquire" in call_order
        assert "load" in call_order
        assert "release" in call_order
        acquire_idx = call_order.index("acquire")
        load_idx = call_order.index("load")
        release_idx = call_order.index("release")
        assert acquire_idx < load_idx, "Lock must be acquired before load"
        assert load_idx < release_idx, "Lock must be released after load"

    @patch("dango.ingestion.dlt_runner.get_source_metadata")
    @patch("dlt.pipeline")
    @patch("dango.ingestion.dlt_runner.os.chdir")
    def test_extract_failure_no_load(
        self,
        mock_chdir,
        mock_dlt_pipeline_cls,
        mock_get_meta,
        tmp_path,
    ):
        """If extract fails, load should never be called and lock never acquired."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        mock_get_meta.return_value = {
            "dlt_package": "dlt.sources.test",
            "dlt_function": "test_source",
        }
        runner = DltPipelineRunner(tmp_path)
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)

        source_config = _make_source_config()
        pipeline = _make_mock_pipeline()
        pipeline.extract.side_effect = RuntimeError("API connection failed")
        mock_dlt_pipeline_cls.return_value = pipeline

        with (
            patch.object(_dbt_lock_module, "DbtLock") as mock_lock_cls,
            patch.object(runner, "_load_dlt_source", return_value=MagicMock()),
            patch.object(runner, "_backup_dlt_state", return_value=tmp_path / "backup"),
            patch.object(runner, "_restore_dlt_state"),
            patch.object(runner, "_get_source_total_rows", return_value=100),
            patch.object(runner, "_get_source_table_rows", return_value={}),
            patch.object(runner, "_detect_write_disposition", return_value=False),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", return_value={}),
            patch.object(runner, "_get_dataset_name", return_value="raw_test_source"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            with pytest.raises(RuntimeError, match="API connection failed"):
                runner._run_dlt_source(source_config)

        # Extract was attempted
        pipeline.extract.assert_called()
        # Load was never called
        pipeline.load.assert_not_called()
        # Lock was never acquired
        mock_lock_cls.return_value.acquire.assert_not_called()

    @patch("dango.ingestion.dlt_runner.get_source_metadata")
    @patch("dlt.pipeline")
    @patch("dango.ingestion.dlt_runner.os.chdir")
    def test_normalize_failure_no_load(
        self,
        mock_chdir,
        mock_dlt_pipeline_cls,
        mock_get_meta,
        tmp_path,
    ):
        """If normalize fails, load should never be called."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        mock_get_meta.return_value = {
            "dlt_package": "dlt.sources.test",
            "dlt_function": "test_source",
        }
        runner = DltPipelineRunner(tmp_path)
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)

        source_config = _make_source_config()
        pipeline = _make_mock_pipeline()
        pipeline.normalize.side_effect = RuntimeError("Normalize failed")
        mock_dlt_pipeline_cls.return_value = pipeline

        with (
            patch.object(_dbt_lock_module, "DbtLock") as mock_lock_cls,
            patch.object(runner, "_load_dlt_source", return_value=MagicMock()),
            patch.object(runner, "_backup_dlt_state", return_value=tmp_path / "backup"),
            patch.object(runner, "_restore_dlt_state"),
            patch.object(runner, "_get_source_total_rows", return_value=100),
            patch.object(runner, "_get_source_table_rows", return_value={}),
            patch.object(runner, "_detect_write_disposition", return_value=False),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", return_value={}),
            patch.object(runner, "_get_dataset_name", return_value="raw_test_source"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            with pytest.raises(RuntimeError, match="Normalize failed"):
                runner._run_dlt_source(source_config)

        # Extract was called
        pipeline.extract.assert_called()
        # Normalize was attempted
        pipeline.normalize.assert_called()
        # Load was never called
        pipeline.load.assert_not_called()
        # Lock was never acquired
        mock_lock_cls.return_value.acquire.assert_not_called()

    @patch("dango.ingestion.dlt_runner.get_source_metadata")
    @patch("dlt.pipeline")
    @patch("dango.ingestion.dlt_runner.os.chdir")
    def test_load_failure_lock_released(
        self,
        mock_chdir,
        mock_dlt_pipeline_cls,
        mock_get_meta,
        tmp_path,
    ):
        """If pipeline.load() fails, the lock must still be released."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        mock_get_meta.return_value = {
            "dlt_package": "dlt.sources.test",
            "dlt_function": "test_source",
        }
        runner = DltPipelineRunner(tmp_path)
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)

        source_config = _make_source_config()
        pipeline = _make_mock_pipeline()
        pipeline.load.side_effect = RuntimeError("DuckDB write failed")
        mock_dlt_pipeline_cls.return_value = pipeline

        with (
            patch.object(_dbt_lock_module, "DbtLock") as mock_lock_cls,
            patch.object(runner, "_load_dlt_source", return_value=MagicMock()),
            patch.object(runner, "_backup_dlt_state", return_value=tmp_path / "backup"),
            patch.object(runner, "_restore_dlt_state"),
            patch.object(runner, "_get_source_total_rows", return_value=100),
            patch.object(runner, "_get_source_table_rows", return_value={}),
            patch.object(runner, "_detect_write_disposition", return_value=False),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", return_value={}),
            patch.object(runner, "_get_dataset_name", return_value="raw_test_source"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            with pytest.raises(RuntimeError, match="DuckDB write failed"):
                runner._run_dlt_source(source_config)

        # Lock was acquired for load
        mock_lock_cls.return_value.acquire.assert_called_once()
        # Lock was released (in finally block)
        mock_lock_cls.return_value.release.assert_called_once()

    @patch("dango.ingestion.dlt_runner.get_source_metadata")
    @patch("dlt.pipeline")
    @patch("dango.ingestion.dlt_runner.os.chdir")
    def test_max_lock_wait_passed_to_lock(
        self,
        mock_chdir,
        mock_dlt_pipeline_cls,
        mock_get_meta,
        tmp_path,
    ):
        """max_lock_wait should be passed to DbtLock.acquire()."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        mock_get_meta.return_value = {
            "dlt_package": "dlt.sources.test",
            "dlt_function": "test_source",
        }
        runner = DltPipelineRunner(tmp_path)
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)

        source_config = _make_source_config()
        pipeline = _make_mock_pipeline()
        mock_dlt_pipeline_cls.return_value = pipeline

        with (
            patch.object(_dbt_lock_module, "DbtLock") as mock_lock_cls,
            patch.object(runner, "_load_dlt_source", return_value=MagicMock()),
            patch.object(runner, "_backup_dlt_state", return_value=None),
            patch.object(runner, "_get_source_total_rows", return_value=0),
            patch.object(runner, "_get_source_table_rows", return_value={}),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}),
            patch.object(runner, "_detect_write_disposition", return_value=False),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", return_value={}),
            patch.object(runner, "_get_dataset_name", return_value="raw_test_source"),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_cleanup_state_backup"),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            runner._run_dlt_source(source_config, max_lock_wait=45)

        mock_lock_cls.return_value.acquire.assert_called_once_with(timeout=45)


@pytest.mark.unit
class TestDltNativeSourcePhases:
    """Tests for the extract/normalize/load split in _run_dlt_native_source()."""

    @patch("dlt.pipeline")
    @patch("dango.ingestion.dlt_runner.os.chdir")
    def test_native_source_mirrors_split(
        self,
        mock_chdir,
        mock_dlt_pipeline_cls,
        tmp_path,
    ):
        """_run_dlt_native_source() must have same extract/normalize/lock/load split."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner(tmp_path)
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)

        source_config = MagicMock()
        source_config.name = "test_native"
        source_config.type.value = "dlt_native"
        source_config.dlt_native.source_module = "test_module"
        source_config.dlt_native.source_function = "test_func"
        source_config.dlt_native.function_kwargs = {}
        source_config.dlt_native.dataset_name = None
        source_config.dlt_native.pipeline_name = "test_native"

        pipeline = _make_mock_pipeline()
        mock_dlt_pipeline_cls.return_value = pipeline

        with (
            patch.object(_dbt_lock_module, "DbtLock") as mock_lock_cls,
            patch("importlib.import_module") as mock_import,
            patch.object(runner, "_backup_dlt_state", return_value=None),
            patch.object(runner, "_get_source_total_rows", return_value=100),
            patch.object(runner, "_get_source_table_rows", return_value={}),
            patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 50}),
            patch.object(runner, "_detect_write_disposition", return_value=False),
            patch.object(runner, "_cleanup_state_backup"),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            mock_module = MagicMock()
            mock_module.test_func.return_value = MagicMock()
            mock_import.return_value = mock_module
            runner._run_dlt_native_source(source_config)

        # Extract should have been called
        pipeline.extract.assert_called_once()
        # Normalize should have been called
        pipeline.normalize.assert_called_once()
        # Load should have been called
        pipeline.load.assert_called_once()
        # Lock was acquired for load
        mock_lock_cls.return_value.acquire.assert_called_once()
        # Lock was released after load
        mock_lock_cls.return_value.release.assert_called_once()
