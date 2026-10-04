"""tests/unit/test_empty_sync_policy.py

Unit tests for the persisted per-source `empty_sync_policy` field and the
tri-state `allow_empty_replace` resolution in `DltPipelineRunner.run_source()`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dango.config.models import DataSource, SourceType


def _make_runner(tmp_path: Path):
    """Create a DltPipelineRunner with mocked internals."""
    from dango.ingestion.dlt_runner import DltPipelineRunner

    runner = DltPipelineRunner.__new__(DltPipelineRunner)
    runner.project_root = tmp_path
    runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
    runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    runner._current_oauth_warning = None
    return runner


def _make_csv_source(name: str = "csv_source", empty_sync_policy: str | None = None) -> DataSource:
    """Build a real CSV DataSource, optionally setting empty_sync_policy."""
    kwargs = {"name": name, "type": SourceType.CSV}
    if empty_sync_policy is not None:
        kwargs["empty_sync_policy"] = empty_sync_policy
    return DataSource(**kwargs)


@pytest.mark.unit
def test_data_source_empty_sync_policy_defaults_to_block():
    """A DataSource built from a mapping with no `empty_sync_policy` key defaults to
    "block" — the backward-compatibility case for every pre-1.0.10 sources.yml."""
    source = DataSource.model_validate({"name": "legacy_source", "type": "csv"})
    assert source.empty_sync_policy == "block"


@pytest.mark.unit
class TestRunSourceEmptySyncPolicyResolution:
    """run_source()'s tri-state resolution of allow_empty_replace against empty_sync_policy.

    Dispatches through the CSV path (the simplest, no `_run_with_timeout` wrapper) and
    replaces `_run_csv_source` with a fake that reports blocked/not-blocked based on the
    `allow_empty_replace` value it actually receives — this isolates the resolution logic
    added to `run_source()` from the (already covered, untouched) 0-row detection logic
    inside the four `_run_*_source` methods.
    """

    def _run(self, tmp_path: Path, source: DataSource, allow_empty_replace: bool | None):
        runner = _make_runner(tmp_path)

        def fake_run_csv_source(config, full_refresh, allow_empty_replace):
            return {
                "status": "success" if allow_empty_replace else "failed",
                "source": config.name,
                "rows_loaded": 0,
                "error": None if allow_empty_replace else "0 rows, existing data preserved",
            }

        with (
            patch.object(runner, "_run_csv_source", side_effect=fake_run_csv_source) as mock_run,
            patch("dango.utils.db_health.check_disk_space", return_value=True),
            patch(
                "dango.utils.db_health.check_duckdb_health",
                return_value={"status": "new", "size_gb": 0},
            ),
            patch("dango.utils.activity_log.log_activity"),
            patch("dango.utils.sync_history.save_sync_history_entry"),
        ):
            result = runner.run_source(source, allow_empty_replace=allow_empty_replace)

        return result, mock_run

    def test_run_source_none_resolves_to_allow_policy(self, tmp_path):
        """empty_sync_policy='allow' + allow_empty_replace=None -> resolves to True, not blocked."""
        source = _make_csv_source(empty_sync_policy="allow")
        result, mock_run = self._run(tmp_path, source, allow_empty_replace=None)

        assert mock_run.call_args.kwargs["allow_empty_replace"] is True
        assert result["status"] == "success"

    def test_run_source_none_resolves_to_block_policy(self, tmp_path):
        """empty_sync_policy='block' (default) + allow_empty_replace=None -> resolves to False,
        still blocks exactly like pre-1.0.10 behavior."""
        source = _make_csv_source(empty_sync_policy="block")
        result, mock_run = self._run(tmp_path, source, allow_empty_replace=None)

        assert mock_run.call_args.kwargs["allow_empty_replace"] is False
        assert result["status"] == "failed"

    def test_run_source_explicit_false_overrides_allow_policy(self, tmp_path):
        """empty_sync_policy='allow' but allow_empty_replace=False passed explicitly ->
        explicit override wins, still blocks."""
        source = _make_csv_source(empty_sync_policy="allow")
        result, mock_run = self._run(tmp_path, source, allow_empty_replace=False)

        assert mock_run.call_args.kwargs["allow_empty_replace"] is False
        assert result["status"] == "failed"

    def test_run_source_explicit_true_overrides_block_policy(self, tmp_path):
        """empty_sync_policy='block' but allow_empty_replace=True passed explicitly ->
        today's existing escape-hatch behavior, unchanged: does not block."""
        source = _make_csv_source(empty_sync_policy="block")
        result, mock_run = self._run(tmp_path, source, allow_empty_replace=True)

        assert mock_run.call_args.kwargs["allow_empty_replace"] is True
        assert result["status"] == "success"
