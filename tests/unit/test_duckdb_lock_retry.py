"""tests/unit/test_duckdb_lock_retry.py

Unit tests for DuckDB lock conflict retry in _load_with_lock().
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from dlt.destinations.exceptions import DestinationConnectionError

import dango.utils.dbt_lock  # noqa: F401 — force submodule into sys.modules

# dango.utils.__init__ exports a *function* named dbt_lock which shadows the
# submodule dango.utils.dbt_lock. Fetch the module from sys.modules to get
# the real module for patch.object().
_dbt_lock_module = sys.modules["dango.utils.dbt_lock"]


def _make_runner(tmp_path):
    """Build a DltPipelineRunner-like object with a real, bound _load_with_lock()."""
    from dango.ingestion.dlt_runner import DltPipelineRunner

    runner = MagicMock(spec=DltPipelineRunner)
    runner.project_root = tmp_path
    runner._load_with_lock = DltPipelineRunner._load_with_lock.__get__(runner)
    return runner


def _lock_error(
    reason: str = "Could not set lock on file warehouse.duckdb: Conflicting lock is held in Python (PID 1234)",
):
    return DestinationConnectionError(
        client_type="duckdb", dataset_name="main", reason=reason, inner_exc=Exception(reason)
    )


@pytest.mark.unit
class TestLoadWithLockRetry:
    """_load_with_lock retries on DuckDB file lock conflicts."""

    def test_retries_on_lock_conflict(self, tmp_path):
        """Retries pipeline.load() when DuckDB lock conflict is detected."""
        runner = _make_runner(tmp_path)
        pipeline = MagicMock()
        success_result = MagicMock()
        pipeline.load.side_effect = [_lock_error(), success_result]

        with (
            patch(
                "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes",
                return_value=False,
            ),
            patch.object(_dbt_lock_module, "DbtLock") as mock_lock_cls,
            patch("dango.ingestion.dlt_runner.time.sleep") as mock_sleep,
        ):
            mock_lock_cls.return_value.acquire.return_value = True

            result = runner._load_with_lock(pipeline, "test_source")

        assert result == success_result
        assert pipeline.load.call_count == 2
        mock_sleep.assert_called_once_with(10)

    def test_raises_after_max_retries(self, tmp_path):
        """Raises DestinationConnectionError after all retries exhausted."""
        runner = _make_runner(tmp_path)
        pipeline = MagicMock()
        pipeline.load.side_effect = _lock_error()

        with (
            patch(
                "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes",
                return_value=False,
            ),
            patch.object(_dbt_lock_module, "DbtLock") as mock_lock_cls,
            patch("dango.ingestion.dlt_runner.time.sleep"),
        ):
            mock_lock_cls.return_value.acquire.return_value = True

            with pytest.raises(DestinationConnectionError, match="Could not set lock"):
                runner._load_with_lock(pipeline, "test_source")

        assert pipeline.load.call_count == 5  # _LOCK_MAX_RETRIES

    def test_non_lock_error_not_retried(self, tmp_path):
        """DestinationConnectionError without the lock message is raised immediately."""
        runner = _make_runner(tmp_path)
        pipeline = MagicMock()
        pipeline.load.side_effect = _lock_error(reason="Wrong credentials")

        with (
            patch(
                "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes",
                return_value=False,
            ),
            patch.object(_dbt_lock_module, "DbtLock") as mock_lock_cls,
            patch("dango.ingestion.dlt_runner.time.sleep") as mock_sleep,
        ):
            mock_lock_cls.return_value.acquire.return_value = True

            with pytest.raises(DestinationConnectionError, match="Wrong credentials"):
                runner._load_with_lock(pipeline, "test_source")

        assert pipeline.load.call_count == 1  # no retry
        mock_sleep.assert_not_called()
