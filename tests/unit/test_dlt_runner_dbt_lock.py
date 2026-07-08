"""tests/unit/test_dlt_runner_dbt_lock.py

Tests for DbtLock acquisition around run_dbt_models() in run_sync(). BUG-S3-2.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

import dango.utils.dbt_lock  # noqa: F401 — force submodule into sys.modules

# dango.utils.__init__ exports a *function* named dbt_lock which shadows the
# submodule dango.utils.dbt_lock.  Fetch the module from sys.modules to get
# the real module for patch.object().
_dbt_lock_module = sys.modules["dango.utils.dbt_lock"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source_config(name="test_source", source_type_value="hubspot"):
    """Create a minimal DataSource mock."""
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
class TestRunSyncDbtLock:
    """Tests for DbtLock around dbt transforms in run_sync()."""

    def test_dbt_lock_acquired_before_dbt_and_released_after(self, tmp_path):
        """run_sync() must acquire DbtLock around run_dbt_models()."""
        from dango.ingestion.dlt_runner import run_sync

        source_config = _make_source_config()

        # Mock runner so run_source() returns success
        mock_runner = MagicMock()
        mock_runner.run_source.return_value = {
            "status": "success",
            "source": "test_src",
            "rows_loaded": 100,
        }

        # Mock staging model generation
        mock_generator = MagicMock()
        mock_generator.generate_all_models.return_value = {
            "generated": [],
            "skipped": [],
        }

        call_order = []

        def _track_acquire(*args, **kwargs):
            call_order.append("acquire")
            return True

        def _track_dbt(*args, **kwargs):
            call_order.append("dbt")
            return (True, "OK")

        def _track_release(*args, **kwargs):
            call_order.append("release")

        with (
            patch(
                "dango.ingestion.dlt_runner.DltPipelineRunner",
                return_value=mock_runner,
            ),
            patch(
                "dango.transformation.generator.DbtModelGenerator",
                return_value=mock_generator,
            ),
            patch(
                "dango.governance.schema_drift.detect_drift_for_sources",
                return_value=[],
            ),
            patch(
                "dango.transformation.run_dbt_models",
                side_effect=_track_dbt,
            ),
            patch(
                "dango.transformation.generate_dbt_docs",
                return_value=(True, ""),
            ),
            patch(
                "dango.visualization.metabase.refresh_metabase_connection",
                return_value=(False, None),
            ),
            patch(
                "dango.visualization.metabase.sync_metabase_schema",
                return_value=False,
            ),
            patch(
                "dango.utils.post_sync.dispatch_post_sync_hooks",
                return_value=None,
            ),
            patch("duckdb.connect") as mock_duckdb_connect,
            patch.object(_dbt_lock_module, "DbtLock") as mock_lock_cls,
            patch("dango.ingestion.dlt_runner.console"),
        ):
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = []
            mock_duckdb_connect.return_value.__enter__.return_value = mock_conn

            mock_lock_cls.return_value.acquire.side_effect = _track_acquire
            mock_lock_cls.return_value.release.side_effect = _track_release

            run_sync(
                project_root=tmp_path,
                sources=[source_config],
                max_lock_wait=45,
            )

        # Verify lock acquire → dbt → lock release ordering
        assert "acquire" in call_order
        assert "dbt" in call_order
        assert "release" in call_order
        acquire_idx = call_order.index("acquire")
        dbt_idx = call_order.index("dbt")
        release_idx = call_order.index("release")
        assert acquire_idx < dbt_idx, "Lock must be acquired before dbt"
        assert dbt_idx < release_idx, "Lock must be released after dbt"

        # Verify DbtLock was constructed with correct args
        mock_lock_cls.assert_called_once_with(tmp_path, source="sync", operation="dbt run")

        # Verify max_lock_wait is passed through to lock.acquire()
        mock_lock_cls.return_value.acquire.assert_called_once_with(timeout=45)

    def test_dbt_lock_released_on_dbt_failure(self, tmp_path):
        """If run_dbt_models() fails, the lock must still be released."""
        from dango.ingestion.dlt_runner import run_sync

        source_config = _make_source_config()

        mock_runner = MagicMock()
        mock_runner.run_source.return_value = {
            "status": "success",
            "source": "test_src",
            "rows_loaded": 100,
        }

        mock_generator = MagicMock()
        mock_generator.generate_all_models.return_value = {
            "generated": [],
            "skipped": [],
        }

        with (
            patch(
                "dango.ingestion.dlt_runner.DltPipelineRunner",
                return_value=mock_runner,
            ),
            patch(
                "dango.transformation.generator.DbtModelGenerator",
                return_value=mock_generator,
            ),
            patch(
                "dango.governance.schema_drift.detect_drift_for_sources",
                return_value=[],
            ),
            patch(
                "dango.transformation.run_dbt_models",
                return_value=(False, "dbt error"),
            ),
            patch(
                "dango.transformation.generate_dbt_docs",
                return_value=(True, ""),
            ),
            patch(
                "dango.visualization.metabase.refresh_metabase_connection",
                return_value=(False, None),
            ),
            patch(
                "dango.visualization.metabase.sync_metabase_schema",
                return_value=False,
            ),
            patch(
                "dango.utils.post_sync.dispatch_post_sync_hooks",
                return_value=None,
            ),
            patch("duckdb.connect") as mock_duckdb_connect,
            patch.object(_dbt_lock_module, "DbtLock") as mock_lock_cls,
            patch("dango.ingestion.dlt_runner.console"),
        ):
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = []
            mock_duckdb_connect.return_value.__enter__.return_value = mock_conn

            run_sync(
                project_root=tmp_path,
                sources=[source_config],
            )

        # Lock was acquired
        mock_lock_cls.return_value.acquire.assert_called_once()
        # Lock was released even though dbt failed
        mock_lock_cls.return_value.release.assert_called_once()
