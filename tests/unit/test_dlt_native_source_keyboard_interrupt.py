"""tests/unit/test_dlt_native_source_keyboard_interrupt.py

1.0.10-S14: _run_dlt_native_source() must have the same KeyboardInterrupt
handler as its sibling _run_dlt_source() (dango/ingestion/dlt_runner.py).
Split into its own file rather than added to test_dlt_runner_pipeline_phases.py
(already 475 lines, within the ~80-line buffer of the 500-line hard limit
enforced by scripts/check_file_sizes.py, which also scans tests/) or
test_empty_replace_protection.py (already far over 500 lines).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import dlt
import pytest

import dango.utils.dbt_lock  # noqa: F401 — force submodule into sys.modules

# dango.utils.__init__ exports a *function* named dbt_lock which shadows the
# submodule dango.utils.dbt_lock. Fetch the module from sys.modules to get
# the real module for patch.object().
_dbt_lock_module = sys.modules["dango.utils.dbt_lock"]


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


def _make_native_source_config(name="test_native"):
    src = MagicMock()
    src.name = name
    src.type.value = "dlt_native"
    src.dlt_native.source_module = "test_module"
    src.dlt_native.source_function = "test_func"
    src.dlt_native.function_kwargs = {}
    src.dlt_native.dataset_name = None
    src.dlt_native.pipeline_name = name
    return src


def _make_hosted_source_config(name="test_source"):
    """Non-native DataSource mock, for the _run_dlt_source() parity side."""
    src = MagicMock()
    src.name = name
    src.type = MagicMock()
    src.type.value = "hubspot"
    src.enabled = True
    src.csv = None
    src.dlt_native = None
    return src


@pytest.mark.unit
class TestNativeSourceKeyboardInterrupt:
    """_run_dlt_native_source()'s KeyboardInterrupt handler (1.0.10-S14)."""

    @patch("dlt.pipeline")
    @patch("dango.ingestion.dlt_runner.os.chdir")
    def test_native_source_keyboard_interrupt_cleans_up_backup(
        self,
        mock_chdir,
        mock_dlt_pipeline_cls,
        tmp_path,
    ):
        """A KeyboardInterrupt during extract/normalize/load must clean up
        the state backup (NOT restore it — the point is to keep dlt's own
        progress so a resume can pick up where it left off) and return an
        'interrupted' result, mirroring _run_dlt_source()'s existing
        KeyboardInterrupt handler."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner(tmp_path)
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)

        source_config = _make_native_source_config()
        pipeline = _make_mock_pipeline()
        mock_dlt_pipeline_cls.return_value = pipeline
        backup_path = tmp_path / "backup"

        with (
            patch.object(_dbt_lock_module, "DbtLock"),
            patch("importlib.import_module") as mock_import,
            patch.object(runner, "_backup_dlt_state", return_value=backup_path),
            patch.object(runner, "_get_source_total_rows", return_value=100),
            patch.object(runner, "_get_source_table_rows", return_value={}),
            patch.object(runner, "_detect_write_disposition", return_value=False),
            patch.object(runner, "_run_extract_with_retry", side_effect=KeyboardInterrupt),
            patch.object(runner, "_cleanup_state_backup") as mock_cleanup,
            patch.object(runner, "_restore_dlt_state") as mock_restore,
            patch("dango.ingestion.dlt_runner.console"),
        ):
            mock_module = MagicMock()
            mock_module.test_func.return_value = MagicMock()
            mock_import.return_value = mock_module
            result = runner._run_dlt_native_source(source_config)

        mock_cleanup.assert_called_once_with(backup_path)
        mock_restore.assert_not_called()
        assert result == {
            "status": "interrupted",
            "source": "test_native",
            "rows_loaded": 0,
            "uses_replace_mode": False,
        }

    @patch("dango.ingestion.dlt_runner.get_source_metadata")
    @patch("dlt.pipeline")
    @patch("dango.ingestion.dlt_runner.os.chdir")
    def test_native_source_interrupt_result_matches_dlt_source_shape(
        self,
        mock_chdir,
        mock_dlt_pipeline_cls,
        mock_get_meta,
        tmp_path,
    ):
        """Parity test: the 'interrupted' result dict from
        _run_dlt_native_source() must have exactly the same keys — same
        semantics, not just the same behavior — as the equivalent dict from
        _run_dlt_source(). Both methods run the same KeyboardInterrupt through
        the same code path (patching _run_extract_with_retry, which both
        methods call identically) so this asserts genuine parity rather than
        two independently hand-written literals."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        # --- _run_dlt_native_source() side ---
        native_runner = DltPipelineRunner(tmp_path)
        native_runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        native_runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)

        native_source_config = _make_native_source_config()
        native_pipeline = _make_mock_pipeline()
        mock_dlt_pipeline_cls.return_value = native_pipeline

        with (
            patch.object(_dbt_lock_module, "DbtLock"),
            patch("importlib.import_module") as mock_import,
            patch.object(native_runner, "_backup_dlt_state", return_value=tmp_path / "backup"),
            patch.object(native_runner, "_get_source_total_rows", return_value=100),
            patch.object(native_runner, "_get_source_table_rows", return_value={}),
            patch.object(native_runner, "_detect_write_disposition", return_value=False),
            patch.object(native_runner, "_run_extract_with_retry", side_effect=KeyboardInterrupt),
            patch.object(native_runner, "_cleanup_state_backup"),
            patch.object(native_runner, "_restore_dlt_state"),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            mock_module = MagicMock()
            mock_module.test_func.return_value = MagicMock()
            mock_import.return_value = mock_module
            native_result = native_runner._run_dlt_native_source(native_source_config)

        # --- _run_dlt_source() side ---
        source_runner = DltPipelineRunner(tmp_path)
        source_runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        source_runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)

        mock_get_meta.return_value = {
            "dlt_package": "dlt.sources.test",
            "dlt_function": "test_source",
        }
        source_config = _make_hosted_source_config()
        source_pipeline = _make_mock_pipeline()
        mock_dlt_pipeline_cls.return_value = source_pipeline

        with (
            patch.object(_dbt_lock_module, "DbtLock"),
            patch.object(source_runner, "_load_dlt_source", return_value=MagicMock()),
            patch.object(source_runner, "_backup_dlt_state", return_value=tmp_path / "backup2"),
            patch.object(source_runner, "_get_source_total_rows", return_value=100),
            patch.object(source_runner, "_get_source_table_rows", return_value={}),
            patch.object(source_runner, "_detect_write_disposition", return_value=False),
            patch.object(source_runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(source_runner, "_inject_oauth_credentials", return_value={}),
            patch.object(source_runner, "_get_dataset_name", return_value="raw_test_source"),
            patch.object(source_runner, "_build_source_config", return_value={}),
            patch.object(source_runner, "_run_extract_with_retry", side_effect=KeyboardInterrupt),
            patch.object(source_runner, "_cleanup_state_backup"),
            patch.object(source_runner, "_restore_dlt_state"),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            source_result = source_runner._run_dlt_source(source_config)

        assert native_result["status"] == "interrupted"
        assert source_result["status"] == "interrupted"
        assert set(native_result.keys()) == set(source_result.keys()), (
            f"Key mismatch: native-only={set(native_result) - set(source_result)}, "
            f"source-only={set(source_result) - set(native_result)}"
        )
