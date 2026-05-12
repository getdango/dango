"""tests/unit/test_dlt_runner_full_refresh.py

Unit tests for BUG-229: Full refresh state backup order and data loss detection.

Tests cover:
- Backup is called BEFORE drop in the full refresh path
- _restore_dlt_state is called with correct single argument on failure
- Row count warning when new count < previous count
- Backup preserved (not cleaned up) when row count drops
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestFullRefreshBackupOrder:
    """Verify backup happens before drop to prevent data loss."""

    @pytest.mark.parametrize("method_name", ["_run_dlt_native_source", "_run_dlt_source"])
    def test_backup_before_drop_in_source_code(self, method_name):
        """Verify in source code that _backup_dlt_state appears before pipeline.drop()."""
        import inspect

        from dango.ingestion.dlt_runner import DltPipelineRunner

        method = getattr(DltPipelineRunner, method_name)
        source = inspect.getsource(method)
        backup_pos = source.find("_backup_dlt_state")
        drop_pos = source.find("pipeline.drop()")

        assert backup_pos != -1, f"_backup_dlt_state not found in {method_name}"
        assert drop_pos != -1, f"pipeline.drop() not found in {method_name}"
        assert backup_pos < drop_pos, (
            f"In {method_name}: _backup_dlt_state (pos {backup_pos}) must appear before "
            f"pipeline.drop() (pos {drop_pos})"
        )


@pytest.mark.unit
class TestRestoreDltStateCallSignature:
    """Verify _restore_dlt_state is called with correct arguments."""

    def test_restore_accepts_single_argument(self):
        """_restore_dlt_state should accept only backup_dir (1 arg besides self)."""
        import inspect

        from dango.ingestion.dlt_runner import DltPipelineRunner

        sig = inspect.signature(DltPipelineRunner._restore_dlt_state)
        # Parameters: self + backup_dir
        params = list(sig.parameters.keys())
        assert params == ["self", "backup_dir"], f"Expected ['self', 'backup_dir'], got {params}"

    @pytest.mark.parametrize("method_name", ["_run_dlt_native_source", "_run_dlt_source"])
    def test_restore_call_has_one_arg(self, method_name):
        """All _restore_dlt_state calls pass exactly 1 arg (backup_dir)."""
        import ast
        import inspect
        import textwrap

        from dango.ingestion.dlt_runner import DltPipelineRunner

        method = getattr(DltPipelineRunner, method_name)
        source = inspect.getsource(method)
        source = textwrap.dedent(source)
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_restore_dlt_state"
            ):
                # Should have exactly 1 positional arg (backup_dir)
                assert len(node.args) == 1, (
                    f"_restore_dlt_state called with {len(node.args)} args "
                    f"at line {node.lineno} in {method_name}, expected 1"
                )


@pytest.mark.unit
class TestFullRefreshRowCountWarning:
    """Verify row count comparison detects data loss on full refresh."""

    def _make_runner(self, tmp_path):
        """Create a DltPipelineRunner with mocked internals for testing."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        runner.project_root = tmp_path
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        runner._current_oauth_warning = None
        return runner

    def _make_native_source_config(self):
        """Create a minimal source config for _run_dlt_native_source."""
        config = MagicMock()
        config.name = "test_source"
        config.type.value = "dlt_native"
        config.dlt_native.source_module = "test_module"
        config.dlt_native.source_function = "test_func"
        config.dlt_native.pipeline_name = "test_pipeline"
        config.dlt_native.dataset_name = "raw_test"
        config.dlt_native.source_args = {}
        return config

    @patch("dango.ingestion.dlt_runner.console")
    @patch("dango.ingestion.dlt_runner.dlt")
    @patch("dango.ingestion.dlt_runner.importlib")
    @patch("os.chdir")
    @patch("os.getcwd", return_value="/tmp")
    def test_backup_preserved_when_row_count_drops(
        self, mock_getcwd, mock_chdir, mock_importlib, mock_dlt, mock_console, tmp_path
    ):
        """When full refresh loads fewer rows, backup should NOT be cleaned up."""
        runner = self._make_runner(tmp_path)
        config = self._make_native_source_config()
        backup_dir = tmp_path / "test_backup_20260512"

        # Mock the source loading
        mock_source = MagicMock()
        mock_module = MagicMock()
        mock_module.test_func.return_value = mock_source
        mock_importlib.import_module.return_value = mock_module

        # Mock pipeline
        mock_pipeline = MagicMock()
        mock_dlt.pipeline.return_value = mock_pipeline

        # Pre-drop: 68k rows. Post-sync: 19 rows.
        runner._backup_dlt_state = MagicMock(return_value=backup_dir)
        runner._get_source_total_rows = MagicMock(return_value=68000)
        runner._cleanup_state_backup = MagicMock()
        runner._run_with_retry = MagicMock(return_value=MagicMock())
        runner._extract_load_stats = MagicMock(return_value={"rows_loaded": 19})

        result = runner._run_dlt_native_source(config, full_refresh=True)

        # Backup should NOT have been cleaned up (row count dropped)
        runner._cleanup_state_backup.assert_not_called()
        assert result["status"] == "success"
        assert result["rows_loaded"] == 19

    @patch("dango.ingestion.dlt_runner.console")
    @patch("dango.ingestion.dlt_runner.dlt")
    @patch("dango.ingestion.dlt_runner.importlib")
    @patch("os.chdir")
    @patch("os.getcwd", return_value="/tmp")
    def test_backup_cleaned_up_when_row_count_ok(
        self, mock_getcwd, mock_chdir, mock_importlib, mock_dlt, mock_console, tmp_path
    ):
        """When full refresh loads same/more rows, backup should be cleaned up."""
        runner = self._make_runner(tmp_path)
        config = self._make_native_source_config()
        backup_dir = tmp_path / "test_backup"

        mock_source = MagicMock()
        mock_module = MagicMock()
        mock_module.test_func.return_value = mock_source
        mock_importlib.import_module.return_value = mock_module

        mock_pipeline = MagicMock()
        mock_dlt.pipeline.return_value = mock_pipeline

        # Pre-drop: 100 rows. Post-sync: 150 rows (normal).
        runner._backup_dlt_state = MagicMock(return_value=backup_dir)
        runner._get_source_total_rows = MagicMock(return_value=100)
        runner._cleanup_state_backup = MagicMock()
        runner._run_with_retry = MagicMock(return_value=MagicMock())
        runner._extract_load_stats = MagicMock(return_value={"rows_loaded": 150})

        result = runner._run_dlt_native_source(config, full_refresh=True)

        # Backup SHOULD be cleaned up (row count is fine)
        runner._cleanup_state_backup.assert_called_once_with(backup_dir)
        assert result["status"] == "success"

    @pytest.mark.parametrize("method_name", ["_run_dlt_native_source", "_run_dlt_source"])
    def test_row_count_warning_in_source_code(self, method_name):
        """Verify the row count warning logic exists in both methods."""
        import inspect

        from dango.ingestion.dlt_runner import DltPipelineRunner

        source = inspect.getsource(getattr(DltPipelineRunner, method_name))
        assert "pre_refresh_rows" in source
        assert "rows_loaded < pre_refresh_rows" in source
        assert "State backup preserved" in source
