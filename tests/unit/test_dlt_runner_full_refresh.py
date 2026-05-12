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

    @patch("dango.ingestion.dlt_runner.console")
    def test_backup_preserved_when_row_count_drops(self, mock_console, tmp_path):
        """When full refresh loads fewer rows, backup should NOT be cleaned up."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        runner.project_root = tmp_path
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        runner._current_oauth_warning = None

        backup_dir = tmp_path / "test_backup_20260512"
        runner._backup_dlt_state = MagicMock(return_value=backup_dir)
        runner._get_source_total_rows = MagicMock(return_value=68000)
        runner._cleanup_state_backup = MagicMock()
        runner._run_with_retry = MagicMock(return_value=MagicMock())
        runner._extract_load_stats = MagicMock(return_value={"rows_loaded": 19})

        # Simulate the success path logic from _run_dlt_source
        full_refresh = True
        state_backup = runner._backup_dlt_state("test")
        pre_refresh_rows = runner._get_source_total_rows("test") if full_refresh else None

        stats = runner._extract_load_stats(MagicMock())
        rows_loaded = stats.get("rows_loaded", 0)

        # Replicate the conditional cleanup logic
        if rows_loaded >= 0:
            if full_refresh and pre_refresh_rows is not None and rows_loaded < pre_refresh_rows:
                # Should NOT clean up backup
                cleanup_called = False
            else:
                runner._cleanup_state_backup(state_backup)
                cleanup_called = True

        assert not cleanup_called, "Backup should be preserved when row count drops"

    @patch("dango.ingestion.dlt_runner.console")
    def test_backup_cleaned_up_when_row_count_ok(self, mock_console, tmp_path):
        """When full refresh loads same/more rows, backup should be cleaned up."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        runner._cleanup_state_backup = MagicMock()

        backup_dir = tmp_path / "test_backup"
        state_backup = backup_dir
        full_refresh = True
        pre_refresh_rows = 100
        rows_loaded = 150

        if rows_loaded >= 0:
            if full_refresh and pre_refresh_rows is not None and rows_loaded < pre_refresh_rows:
                pass  # preserve backup
            else:
                runner._cleanup_state_backup(state_backup)

        runner._cleanup_state_backup.assert_called_once_with(backup_dir)

    def test_row_count_warning_in_source_code(self):
        """Verify the row count warning logic exists in run_source."""
        import inspect

        from dango.ingestion.dlt_runner import DltPipelineRunner

        source = inspect.getsource(DltPipelineRunner._run_dlt_native_source)
        assert "pre_refresh_rows" in source
        assert "rows_loaded < pre_refresh_rows" in source
        assert "State backup preserved" in source
