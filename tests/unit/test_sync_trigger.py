"""tests/unit/test_sync_trigger.py

Tests for the server-side manual sync runner (TASK-040c + R10-N subprocess).
"""

from __future__ import annotations

import json
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
        mock_lock_cls.return_value.acquire.assert_called_once()
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

        assert result["status"] == "success"
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

    @patch(f"{_PATCH_UTILS}.DbtLock")
    @patch(f"{_PATCH_HISTORY}.record_failure")
    @patch(f"{_PATCH_HISTORY}.record_start", return_value=10)
    @patch(f"{_PATCH_HISTORY}.get_scheduler_db_path")
    @patch(f"{_PATCH_CONFIG}.load_config")
    @patch(f"{_PATCH_OAUTH}.validate_before_sync")
    def test_lock_retry_loop(
        self,
        mock_oauth,
        mock_config,
        mock_db_path,
        mock_start,
        mock_failure,
        mock_lock_cls,
        tmp_path,
    ):
        """With max_lock_wait > 0, should retry before failing."""
        from dango.exceptions import DbtLockError
        from dango.platform.scheduling.sync_trigger import run_manual_sync

        mock_config.return_value = _make_config(["src1"])

        # Fail first 2 attempts, succeed on third
        attempts = [0]

        def _side_effect():
            attempts[0] += 1
            if attempts[0] < 3:
                raise DbtLockError("lock held")

        mock_lock_cls.return_value.acquire.side_effect = _side_effect

        with (
            patch(f"{_PATCH_INGESTION}.run_sync", return_value={"results": [], "failed_count": 0}),
            patch(f"{_PATCH_HISTORY}.record_completion"),
            patch(f"{_PATCH_HISTORY}.time.sleep"),
        ):
            result = run_manual_sync(tmp_path, sources=["src1"], max_lock_wait=30)

        assert result["status"] == "success"
        assert attempts[0] == 3


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
