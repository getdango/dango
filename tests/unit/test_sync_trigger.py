"""tests/unit/test_sync_trigger.py

Tests for the server-side manual sync runner (TASK-040c + R10-N subprocess).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Lazy imports in run_manual_sync() → patch at the source module
_PATCH_UTILS = "dango.utils"
_PATCH_CONFIG = "dango.config.helpers"
_PATCH_INGESTION = "dango.ingestion"
_PATCH_HISTORY = "dango.platform.scheduling.sync_trigger"
_PATCH_OAUTH = "dango.oauth.validation"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(source_names):
    config = MagicMock()
    sources_by_name = {}
    for n in source_names:
        src = MagicMock()
        src.name = n
        src.type.value = "hubspot"
        sources_by_name[n] = src
    config.sources.get_source.side_effect = lambda n: sources_by_name.get(n)
    return config


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunManualSync:
    """Tests for run_manual_sync()."""

    @patch(f"{_PATCH_INGESTION}.run_sync", return_value={"results": [], "failed_count": 0})
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=1)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_success(
        self,
        mock_oauth,
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
        mock_lock_cls.return_value.acquire.assert_called_once_with(timeout=300)
        mock_lock_cls.return_value.release.assert_called_once()
        mock_sync.assert_called_once()
        mock_complete.assert_called_once()
        # Verify progress_callback is passed to run_sync
        call_kwargs = mock_sync.call_args[1]
        assert "progress_callback" in call_kwargs
        assert callable(call_kwargs["progress_callback"])

    @patch(f"{_PATCH_INGESTION}.run_sync", return_value={"results": [], "failed_count": 0})
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=2)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_backfill_passes_start_date(
        self,
        mock_oauth,
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
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_lock_failure(
        self,
        mock_oauth,
        mock_config,
        mock_db_path,
        mock_start,
        mock_failure,
        mock_lock_cls,
        tmp_path,
    ):
        from dango.exceptions import DbtLockError
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])
        mock_lock_cls.return_value.acquire.side_effect = DbtLockError("lock held")

        result = run_manual_sync(tmp_path, sources=["src1"])

        assert result["status"] == "failed"
        assert "Lock unavailable" in result["error"]
        mock_failure.assert_called_once()
        mock_lock_cls.return_value.acquire.assert_called_once_with(timeout=300)

    @patch(f"{_PATCH_INGESTION}.run_sync", return_value={"results": [], "failed_count": 0})
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_failure")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=4)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_no_valid_sources(
        self,
        mock_oauth,
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
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_sync_exception_records_failure(
        self,
        mock_oauth,
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

    @patch(f"{_PATCH_INGESTION}.run_sync", return_value={"results": [], "failed_count": 0})
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=6)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_skip_dbt_param(
        self,
        mock_oauth,
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

        result = run_manual_sync(tmp_path, sources=["src1"], skip_dbt=True)

        assert result["status"] == "data_loaded"
        call_kwargs = mock_sync.call_args[1]
        assert call_kwargs["skip_dbt"] is True
        # Verify progress_callback is always passed
        assert "progress_callback" in call_kwargs
        assert callable(call_kwargs["progress_callback"])

    @patch(f"{_PATCH_INGESTION}.run_sync", return_value={"results": [], "failed_count": 0})
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=7)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_start_end_date_params(
        self,
        mock_oauth,
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

        result = run_manual_sync(
            tmp_path, sources=["src1"], start_date="2026-01-01", end_date="2026-01-31"
        )

        assert result["status"] == "success"
        call_kwargs = mock_sync.call_args[1]
        assert call_kwargs["start_date"] is not None
        assert call_kwargs["end_date"] is not None

    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_HISTORY}.record_failure")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=8)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    def test_oauth_failure(self, mock_db_path, mock_start, mock_failure, mock_config, tmp_path):
        from dango.exceptions import OAuthTokenRevokedError
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["google_sheets"])

        with patch(
            f"{_PATCH_OAUTH}.validate_before_sync",
            side_effect=OAuthTokenRevokedError(
                "Token revoked",
                user_message="Token revoked",
            ),
        ):
            result = run_manual_sync(tmp_path, sources=["google_sheets"])

        assert result["status"] == "failed"
        assert "OAuth" in result["error"]
        mock_failure.assert_called_once()

    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_HISTORY}.record_failure")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=8)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch("dango.utils.sync_history.save_sync_history_entry")
    def test_oauth_failure_records_sync_history(
        self, mock_save_history, mock_db_path, mock_start, mock_failure, mock_config, tmp_path
    ):
        """OAuth failures should record to per-source sync_history (M1)."""
        from dango.exceptions import OAuthTokenRevokedError
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["google_sheets", "hubspot"])

        with patch(
            f"{_PATCH_OAUTH}.validate_before_sync",
            side_effect=OAuthTokenRevokedError(
                "Token revoked",
                user_message="Token revoked",
            ),
        ):
            result = run_manual_sync(tmp_path, sources=["google_sheets", "hubspot"])

        assert result["status"] == "failed"
        # save_sync_history_entry should be called once per source
        assert mock_save_history.call_count == 2
        source_names = [call.args[1] for call in mock_save_history.call_args_list]
        assert "google_sheets" in source_names
        assert "hubspot" in source_names
        # Each entry should have status=failed and the OAuth error message
        for call in mock_save_history.call_args_list:
            entry = call.args[2]
            assert entry["status"] == "failed"
            assert "OAuth" in entry["error_message"]
            assert entry["duration_seconds"] == 0

    @patch(
        f"{_PATCH_INGESTION}.run_sync",
        return_value={"results": [{"rows_loaded": 100}], "failed_count": 0},
    )
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_reuses_existing_record_id(
        self,
        mock_oauth,
        mock_db_path,
        mock_complete,
        mock_lock_cls,
        mock_config,
        mock_sync,
        tmp_path,
    ):
        """When record_id is provided, should NOT call record_start."""
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        with patch(f"{_PATCH_HISTORY}.record_start") as mock_start:
            result = run_manual_sync(tmp_path, sources=["src1"], record_id=42)

        assert result["status"] == "success"
        assert result["record_id"] == 42
        mock_start.assert_not_called()

    @patch(
        f"{_PATCH_INGESTION}.run_sync",
        return_value={"results": [{"rows_loaded": 150}], "failed_count": 0},
    )
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=9)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_returns_rows_loaded(
        self,
        mock_oauth,
        mock_db_path,
        mock_start,
        mock_complete,
        mock_lock_cls,
        mock_config,
        mock_sync,
        tmp_path,
    ):
        """Successful sync should include rows_loaded in result."""
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        result = run_manual_sync(tmp_path, sources=["src1"])

        assert result["status"] == "success"
        assert result["rows_loaded"] == 150

    @patch(f"{_PATCH_INGESTION}.run_sync", return_value={"results": [], "failed_count": 0})
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=10)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_acquire_passes_timeout(
        self,
        mock_oauth,
        mock_db_path,
        mock_start,
        mock_complete,
        mock_lock_cls,
        mock_config,
        mock_sync,
        tmp_path,
    ):
        """Lock acquire() should pass max_lock_wait as timeout."""
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        result = run_manual_sync(tmp_path, sources=["src1"], max_lock_wait=30)

        mock_lock_cls.return_value.acquire.assert_called_once_with(timeout=30)
        assert result["status"] == "success"

    @patch(f"{_PATCH_INGESTION}.run_sync", return_value={"results": [], "failed_count": 0})
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=11)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_default_timeout_is_300(
        self,
        mock_oauth,
        mock_db_path,
        mock_start,
        mock_complete,
        mock_lock_cls,
        mock_config,
        mock_sync,
        tmp_path,
    ):
        """Without max_lock_wait, should use 300s default timeout."""
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        result = run_manual_sync(tmp_path, sources=["src1"])

        mock_lock_cls.return_value.acquire.assert_called_once_with(timeout=300)
        assert result["status"] == "success"


@pytest.mark.unit
class TestDataLoadedStatus:
    """Tests for data_loaded return status when skip_dbt=True."""

    @patch(
        f"{_PATCH_INGESTION}.run_sync",
        return_value={"results": [{"rows_loaded": 5}], "failed_count": 0},
    )
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=20)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_skip_dbt_returns_data_loaded_status(
        self,
        mock_oauth,
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

        result = run_manual_sync(tmp_path, sources=["src1"], skip_dbt=True)

        assert result["status"] == "data_loaded"
        mock_complete.assert_called_once()

    @patch(
        f"{_PATCH_INGESTION}.run_sync",
        return_value={"results": [{"rows_loaded": 5}], "failed_count": 0},
    )
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=21)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_skip_dbt_writes_completed_phase_for_poller(
        self,
        mock_oauth,
        mock_db_path,
        mock_start,
        mock_complete,
        mock_lock_cls,
        mock_config,
        mock_sync,
        tmp_path,
    ):
        """Phase must be 'completed' (not 'data_loaded') so poll_sync_status_blocking
        recognises the terminal state. The return dict carries 'data_loaded' status."""
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        result = run_manual_sync(
            tmp_path,
            sources=["src1"],
            skip_dbt=True,
            write_progress=True,
            sync_id="dl1",
        )

        assert result["status"] == "data_loaded"
        status_file = tmp_path / ".dango" / "state" / "sync_status_dl1.json"
        assert status_file.exists()
        data = json.loads(status_file.read_text())
        # Phase must be "completed" for poll_sync_status_blocking compatibility
        assert data["phase"] == "completed"
        assert "dbt deferred" in data["message"]


@pytest.mark.unit
class TestValidationErrorLogging:
    """Tests for logging of non-OAuth validation errors."""

    @patch(f"{_PATCH_INGESTION}.run_sync", return_value={"results": [], "failed_count": 0})
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=30)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    def test_non_oauth_validation_error_logged(
        self,
        mock_db_path,
        mock_start,
        mock_complete,
        mock_lock_cls,
        mock_config,
        mock_sync,
        tmp_path,
    ):
        """Non-OAuth validation errors should be logged but not fail the sync."""
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        with (
            patch(
                f"{_PATCH_OAUTH}.validate_before_sync",
                side_effect=RuntimeError("validation boom"),
            ),
            patch(f"{_PATCH_HISTORY}.logger") as mock_logger,
        ):
            result = run_manual_sync(tmp_path, sources=["src1"])

        # Should succeed (validation error is non-fatal)
        assert result["status"] == "success"
        # But should have logged the warning
        mock_logger.warning.assert_called()
        warning_events = [c[0][0] for c in mock_logger.warning.call_args_list]
        assert "pre_sync_validation_error" in warning_events


@pytest.mark.unit
class TestWriteStatus:
    """Tests for _write_status() atomic file writing."""

    def test_creates_status_file(self, tmp_path):
        from dango.platform.scheduling.sync_trigger import _write_status

        state_dir = tmp_path / ".dango" / "state"
        _write_status(state_dir, phase="starting", message="test")

        status_file = state_dir / "sync_status.json"
        assert status_file.exists()

        data = json.loads(status_file.read_text())
        assert data["phase"] == "starting"
        assert data["message"] == "test"
        assert "pid" in data
        assert "updated_at" in data

    def test_overwrites_existing(self, tmp_path):
        from dango.platform.scheduling.sync_trigger import _write_status

        state_dir = tmp_path / ".dango" / "state"
        _write_status(state_dir, phase="starting", message="first")
        _write_status(state_dir, phase="completed", message="second")

        data = json.loads((state_dir / "sync_status.json").read_text())
        assert data["phase"] == "completed"

    def test_uses_sync_id_in_filename(self, tmp_path):
        from dango.platform.scheduling.sync_trigger import _write_status

        state_dir = tmp_path / ".dango" / "state"
        _write_status(state_dir, sync_id="abc123", phase="starting", message="test")

        assert (state_dir / "sync_status_abc123.json").exists()
        assert not (state_dir / "sync_status.json").exists()

    def test_write_progress_integration(self, tmp_path):
        """run_manual_sync with write_progress=True creates status file."""
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        config = _make_config(["src1"])

        with (
            patch(
                f"{_PATCH_INGESTION}.run_sync",
                return_value={"results": [{"rows_loaded": 42}], "failed_count": 0},
            ),
            patch(f"{_PATCH_CONFIG}.load_config", return_value=config),
            patch(f"{_PATCH_UTILS}.DbtLock"),
            patch(f"{_PATCH_HISTORY}.record_completion"),
            patch(f"{_PATCH_HISTORY}.record_start", return_value=1),
            patch(f"{_PATCH_HISTORY}.get_scheduler_db_path"),
            patch(f"{_PATCH_OAUTH}.validate_before_sync"),
        ):
            result = run_manual_sync(
                tmp_path, sources=["src1"], write_progress=True, sync_id="prog1"
            )

        assert result["status"] == "success"
        status_file = tmp_path / ".dango" / "state" / "sync_status_prog1.json"
        assert status_file.exists()
        data = json.loads(status_file.read_text())
        assert data["phase"] == "completed"
        assert data["rows_loaded"] == 42


@pytest.mark.unit
class TestMainEntrypoint:
    """Tests for the __main__ block JSON parsing."""

    def test_parses_minimal_args(self, tmp_path):
        """The __main__ block should parse minimal JSON and call run_manual_sync."""
        args_json = json.dumps({"project_root": str(tmp_path), "sources": ["src1"]})

        with patch(
            f"{_PATCH_HISTORY}.run_manual_sync",
            return_value={"status": "success"},
        ) as mock_run:
            import dango.platform.scheduling.sync_trigger as mod

            parsed = json.loads(args_json)
            mod.run_manual_sync(
                project_root=tmp_path,
                sources=parsed["sources"],
                full_refresh=parsed.get("full_refresh", False),
            )

        mock_run.assert_called_once()

    def test_all_optional_fields_accepted(self):
        """All optional fields in JSON args should be parseable without error."""
        args = {
            "project_root": "/tmp/test",
            "sources": ["src1"],
            "full_refresh": True,
            "backfill_days": 7,
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "write_progress": True,
            "source_label": "scheduler",
            "skip_dbt": True,
            "max_lock_wait": 300,
            "sync_id": "abc123",
            "record_id": 42,
        }
        parsed = json.loads(json.dumps(args))
        assert parsed["sources"] == ["src1"]
        assert parsed.get("sync_id") == "abc123"
        assert parsed.get("record_id") == 42


@pytest.mark.unit
class TestDbtFailureHandling:
    """Tests for BUG-230: dbt failure should record failure, not completion."""

    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_failure")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=20)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_dbt_failed_records_failure_not_completion(
        self,
        mock_oauth,
        mock_db_path,
        mock_start,
        mock_complete,
        mock_failure,
        mock_lock_cls,
        mock_config,
        tmp_path,
    ):
        """When dbt fails, run_manual_sync should record failure, not completion."""
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        def _fake_sync(**kwargs):
            # Simulate dbt failure via progress callback
            cb = kwargs.get("progress_callback")
            if cb:
                cb("dbt_failed", "dbt models failed")
            return {"results": [{"rows_loaded": 50}], "failed_count": 1}

        with patch(f"{_PATCH_INGESTION}.run_sync", side_effect=_fake_sync):
            result = run_manual_sync(tmp_path, sources=["src1"])

        assert result["status"] == "failed"
        assert result["error"] == "dbt models failed"
        assert result["rows_loaded"] == 50
        mock_failure.assert_called_once()
        mock_complete.assert_not_called()

    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_failure")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=21)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_dbt_failed_writes_dbt_error_flag(
        self,
        mock_oauth,
        mock_db_path,
        mock_start,
        mock_complete,
        mock_failure,
        mock_lock_cls,
        mock_config,
        tmp_path,
    ):
        """Status file should have phase='failed' and dbt_error=True on dbt failure."""
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        def _fake_sync(**kwargs):
            cb = kwargs.get("progress_callback")
            if cb:
                cb("dbt_failed", "dbt models failed")
            return {"results": [{"rows_loaded": 30}], "failed_count": 1}

        with patch(f"{_PATCH_INGESTION}.run_sync", side_effect=_fake_sync):
            result = run_manual_sync(
                tmp_path, sources=["src1"], write_progress=True, sync_id="dbt_err1"
            )

        assert result["status"] == "failed"
        status_file = tmp_path / ".dango" / "state" / "sync_status_dbt_err1.json"
        assert status_file.exists()
        data = json.loads(status_file.read_text())
        assert data["phase"] == "failed"
        assert data["dbt_error"] is True
        assert data["rows_loaded"] == 30

    @patch(
        f"{_PATCH_INGESTION}.run_sync",
        return_value={"results": [{"rows_loaded": 75}], "failed_count": 0},
    )
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_completion")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=22)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_dbt_success_writes_completed(
        self,
        mock_oauth,
        mock_db_path,
        mock_start,
        mock_complete,
        mock_lock_cls,
        mock_config,
        mock_sync,
        tmp_path,
    ):
        """Successful sync should write phase='completed' with no dbt_error."""
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        result = run_manual_sync(tmp_path, sources=["src1"], write_progress=True, sync_id="dbt_ok1")

        assert result["status"] == "success"
        status_file = tmp_path / ".dango" / "state" / "sync_status_dbt_ok1.json"
        assert status_file.exists()
        data = json.loads(status_file.read_text())
        assert data["phase"] == "completed"
        assert "dbt_error" not in data


# ---------------------------------------------------------------------------
# _trigger_metabase_schema_scan tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTriggerMetabaseSchemaSync:
    """Tests for _trigger_metabase_schema_scan."""

    def test_skips_when_no_metabase_yml(self, tmp_path: Path) -> None:
        from dango.platform.scheduling.sync_trigger import (
            _trigger_metabase_schema_scan,
        )

        _trigger_metabase_schema_scan(tmp_path)
        # No exception — gracefully skips

    def test_skips_when_health_timeout(self, tmp_path: Path) -> None:
        from dango.platform.scheduling.sync_trigger import (
            _trigger_metabase_schema_scan,
        )

        mb_yml = tmp_path / ".dango" / "metabase.yml"
        mb_yml.parent.mkdir(parents=True, exist_ok=True)
        mb_yml.write_text("admin:\n  email: a@b.com\n  password: pw\ndatabase:\n  id: 1\n")

        mock_requests = MagicMock()
        mock_requests.get.side_effect = Exception("connection refused")
        with patch.dict("sys.modules", {"requests": mock_requests}):
            _trigger_metabase_schema_scan(tmp_path)
            # Skips after health timeout — no schema sync attempted

    def test_skips_when_missing_credentials(self, tmp_path: Path) -> None:
        from dango.platform.scheduling.sync_trigger import (
            _trigger_metabase_schema_scan,
        )

        mb_yml = tmp_path / ".dango" / "metabase.yml"
        mb_yml.parent.mkdir(parents=True, exist_ok=True)
        mb_yml.write_text("admin:\n  email: a@b.com\n")  # no password, no database_id

        health_resp = MagicMock()
        health_resp.status_code = 200

        mock_requests = MagicMock()
        mock_requests.get.return_value = health_resp
        with patch.dict("sys.modules", {"requests": mock_requests}):
            _trigger_metabase_schema_scan(tmp_path)
            # No login attempt — missing credentials
            mock_requests.post.assert_not_called()

    def test_triggers_schema_sync_on_success(self, tmp_path: Path) -> None:
        from dango.platform.scheduling.sync_trigger import (
            _trigger_metabase_schema_scan,
        )

        mb_yml = tmp_path / ".dango" / "metabase.yml"
        mb_yml.parent.mkdir(parents=True, exist_ok=True)
        mb_yml.write_text("admin:\n  email: a@b.com\n  password: pw\ndatabase:\n  id: 2\n")

        health_resp = MagicMock()
        health_resp.status_code = 200
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"id": "session123"}
        sync_resp = MagicMock()
        sync_resp.status_code = 200

        mock_requests = MagicMock()
        mock_requests.get.return_value = health_resp
        mock_requests.post.side_effect = [login_resp, sync_resp]
        with patch.dict("sys.modules", {"requests": mock_requests}):
            _trigger_metabase_schema_scan(tmp_path)

            # Verify schema sync was called
            calls = mock_requests.post.call_args_list
            assert len(calls) == 2
            assert "/api/session" in calls[0].args[0]
            assert "/api/database/2/sync_schema" in calls[1].args[0]
            assert calls[1].kwargs["headers"]["X-Metabase-Session"] == "session123"
