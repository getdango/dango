"""tests/unit/test_sync_trigger.py

Tests for the server-side manual sync runner (TASK-040c).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# Lazy imports in run_manual_sync() → patch at the source module
_PATCH_UTILS = "dango.utils"
_PATCH_CONFIG = "dango.config.helpers"
_PATCH_INGESTION = "dango.ingestion"
_PATCH_HISTORY = "dango.platform.scheduling.sync_trigger"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(source_names):
    config = MagicMock()
    sources_by_name = {n: MagicMock(name=n) for n in source_names}
    config.sources.get_source.side_effect = lambda n: sources_by_name.get(n)
    return config


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunManualSync:
    """Tests for run_manual_sync()."""

    @patch(f"{_PATCH_INGESTION}.run_sync")
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=1)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    def test_success(
        self,
        mock_db_path,
        mock_start,
        mock_complete,
        mock_lock_cls,
        mock_config,
        mock_sync,
        tmp_path,
    ):
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        result = run_manual_sync(tmp_path, sources=["src1"])

        assert result["status"] == "success"
        assert result["record_id"] == 1
        assert "duration_seconds" in result
        mock_lock_cls.return_value.acquire.assert_called_once()
        mock_lock_cls.return_value.release.assert_called_once()
        mock_sync.assert_called_once()
        mock_complete.assert_called_once()

    @patch(f"{_PATCH_INGESTION}.run_sync")
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=2)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    def test_backfill_passes_start_date(
        self,
        mock_db_path,
        mock_start,
        mock_complete,
        mock_lock_cls,
        mock_config,
        mock_sync,
        tmp_path,
    ):
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        result = run_manual_sync(tmp_path, sources=["src1"], backfill_days=7)

        assert result["status"] == "success"
        call_kwargs = mock_sync.call_args[1]
        assert call_kwargs["start_date"] is not None

    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_failure")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=3)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    def test_lock_failure(self, mock_db_path, mock_start, mock_failure, mock_lock_cls, tmp_path):
        from dango.exceptions import DbtLockError
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_lock_cls.return_value.acquire.side_effect = DbtLockError("lock held")

        result = run_manual_sync(tmp_path, sources=["src1"])

        assert result["status"] == "failed"
        assert "Lock unavailable" in result["error"]
        mock_failure.assert_called_once()

    @patch(f"{_PATCH_INGESTION}.run_sync")
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_failure")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=4)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    def test_no_valid_sources(
        self,
        mock_db_path,
        mock_start,
        mock_failure,
        mock_lock_cls,
        mock_config,
        mock_sync,
        tmp_path,
    ):
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config([])  # no sources resolve

        result = run_manual_sync(tmp_path, sources=["nonexistent"])

        assert result["status"] == "failed"
        assert "No valid sources" in result["error"]
        mock_failure.assert_called_once()
        mock_sync.assert_not_called()

    @patch(f"{_PATCH_INGESTION}.run_sync", side_effect=RuntimeError("DuckDB crash"))
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_failure")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=5)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    def test_sync_exception_records_failure(
        self,
        mock_db_path,
        mock_start,
        mock_failure,
        mock_lock_cls,
        mock_config,
        mock_sync,
        tmp_path,
    ):
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        result = run_manual_sync(tmp_path, sources=["src1"])

        assert result["status"] == "failed"
        assert "DuckDB crash" in result["error"]
        mock_failure.assert_called_once()
        mock_lock_cls.return_value.release.assert_called_once()
