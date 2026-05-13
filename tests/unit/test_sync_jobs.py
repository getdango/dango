"""tests/unit/test_sync_jobs.py

Tests for dango.platform.scheduling.jobs — scheduled sync and dbt job functions.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest

import dango.utils.dbt_lock  # noqa: F401 — force submodule into sys.modules

# dango.utils.__init__ exports a *function* named ``dbt_lock`` which shadows
# the submodule ``dango.utils.dbt_lock``.  Both string-based patch() and
# ``import X as Y`` resolve to the function.  Fetch the module from
# sys.modules to get the real module object for patch.object().
_dbt_lock_module = sys.modules["dango.utils.dbt_lock"]

_JOBS_MOD = "dango.platform.scheduling.jobs"

# Lazy imports in job functions → patch at origin
_NOTIF_MOD = "dango.platform.notifications.webhook"
_SYNC_PROC_MOD = "dango.platform.sync_process"
_DBT_MOD = "dango.transformation"
_HIST_MOD = "dango.utils.sync_history"
_CFG_MOD = "dango.config.helpers"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(name="test_source", enabled=True):
    """Create a minimal DataSource mock."""
    src = MagicMock()
    src.name = name
    src.enabled = enabled
    return src


def _make_config_with_sources(*source_names):
    """Create a mock load_config that returns sources by name."""
    sources = {n: _make_source(n) for n in source_names}
    config = MagicMock()
    config.sources.get_source.side_effect = lambda n: sources.get(n)
    return config


# ---------------------------------------------------------------------------
# configure_jobs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfigureJobs:
    """Test configure_jobs() stores the event loop."""

    def test_stores_loop(self):
        import dango.platform.scheduling.jobs as jobs_mod

        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        old = jobs_mod._event_loop
        try:
            jobs_mod.configure_jobs(loop)
            assert jobs_mod._event_loop is loop
        finally:
            jobs_mod._event_loop = old

    def test_called_from_scheduler_start(self, tmp_path):
        """SchedulerService.start() should call configure_jobs."""
        from tests.unit.test_scheduler import _make_service

        svc = _make_service(tmp_path)
        loop = MagicMock(spec=asyncio.AbstractEventLoop)

        with patch(f"{_JOBS_MOD}.configure_jobs") as mock_cfg:
            svc.start(loop)

        mock_cfg.assert_called_once_with(loop, svc)


# ---------------------------------------------------------------------------
# _broadcast helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBroadcastHelper:
    """Test _broadcast() async bridge."""

    def test_delegates_to_ws_manager(self):
        import dango.platform.scheduling.jobs as jobs_mod

        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        future = MagicMock()
        old = jobs_mod._event_loop
        try:
            jobs_mod._event_loop = loop

            with patch(f"{_JOBS_MOD}.asyncio") as mock_asyncio:
                mock_asyncio.run_coroutine_threadsafe.return_value = future
                jobs_mod._broadcast({"event": "test"})

            mock_asyncio.run_coroutine_threadsafe.assert_called_once()
            future.result.assert_called_once()
        finally:
            jobs_mod._event_loop = old

    def test_skips_without_loop(self):
        import dango.platform.scheduling.jobs as jobs_mod

        old = jobs_mod._event_loop
        try:
            jobs_mod._event_loop = None
            jobs_mod._broadcast({"event": "test"})
        finally:
            jobs_mod._event_loop = old

    def test_catches_exceptions(self):
        import dango.platform.scheduling.jobs as jobs_mod

        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        old = jobs_mod._event_loop
        try:
            jobs_mod._event_loop = loop
            with patch(f"{_JOBS_MOD}.asyncio") as mock_asyncio:
                mock_asyncio.run_coroutine_threadsafe.side_effect = RuntimeError("closed")
                jobs_mod._broadcast({"event": "test"})
        finally:
            jobs_mod._event_loop = old


# ---------------------------------------------------------------------------
# run_scheduled_sync
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunScheduledSync:
    """Test run_scheduled_sync() job function (subprocess-based)."""

    def test_launches_subprocess_per_source(self, tmp_path):
        config = _make_config_with_sources("src1")

        mock_process = MagicMock()

        with (
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_CFG_MOD}.load_config", return_value=config),
            patch(
                f"{_SYNC_PROC_MOD}.launch_sync_subprocess",
                return_value=(mock_process, "test_id"),
            ) as mock_launch,
            patch(f"{_SYNC_PROC_MOD}.poll_sync_status_blocking", return_value=(True, {})),
            patch(f"{_SYNC_PROC_MOD}.cleanup_sync_status"),
            patch(f"{_HIST_MOD}.save_sync_history_entry"),
            patch(f"{_JOBS_MOD}._broadcast"),
            patch(f"{_JOBS_MOD}._notify"),
            patch(f"{_JOBS_MOD}._check_freshness"),
            patch(f"{_JOBS_MOD}._add_pending_dbt_source"),
            patch(f"{_JOBS_MOD}._run_coalesced_dbt"),
        ):
            from dango.platform.scheduling.jobs import run_scheduled_sync

            run_scheduled_sync("daily", ["src1"], project_root=str(tmp_path))

        mock_launch.assert_called_once()
        call_kwargs = mock_launch.call_args[1]
        assert call_kwargs["sources"] == ["src1"]
        assert call_kwargs["skip_dbt"] is True
        assert call_kwargs["source_label"] == "scheduler"
        assert call_kwargs["max_lock_wait"] == 300

    def test_broadcasts_sync_started_and_completed(self, tmp_path):
        config = _make_config_with_sources("src1")

        with (
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_CFG_MOD}.load_config", return_value=config),
            patch(
                f"{_SYNC_PROC_MOD}.launch_sync_subprocess", return_value=(MagicMock(), "test_id")
            ),
            patch(f"{_SYNC_PROC_MOD}.poll_sync_status_blocking", return_value=(True, {})),
            patch(f"{_SYNC_PROC_MOD}.cleanup_sync_status"),
            patch(f"{_HIST_MOD}.save_sync_history_entry"),
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
            patch(f"{_JOBS_MOD}._notify"),
            patch(f"{_JOBS_MOD}._check_freshness"),
            patch(f"{_JOBS_MOD}._add_pending_dbt_source"),
            patch(f"{_JOBS_MOD}._run_coalesced_dbt"),
        ):
            from dango.platform.scheduling.jobs import run_scheduled_sync

            run_scheduled_sync("daily", ["src1"], project_root=str(tmp_path))

        events = [c.args[0]["event"] for c in mock_bc.call_args_list]
        assert "sync_started" in events
        assert "sync_completed" in events

    def test_subprocess_failure_broadcasts_sync_failed(self, tmp_path):
        config = _make_config_with_sources("src1")

        with (
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_CFG_MOD}.load_config", return_value=config),
            patch(
                f"{_SYNC_PROC_MOD}.launch_sync_subprocess", return_value=(MagicMock(), "test_id")
            ),
            patch(
                f"{_SYNC_PROC_MOD}.poll_sync_status_blocking",
                return_value=(False, {"error": "boom"}),
            ),
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
            patch(f"{_JOBS_MOD}._notify"),
        ):
            from dango.platform.scheduling.jobs import run_scheduled_sync

            run_scheduled_sync("daily", ["src1"], project_root=str(tmp_path))

        events = [c.args[0]["event"] for c in mock_bc.call_args_list]
        assert "sync_failed" in events

    def test_records_history_per_source(self, tmp_path):
        config = _make_config_with_sources("src1", "src2")

        with (
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_CFG_MOD}.load_config", return_value=config),
            patch(
                f"{_SYNC_PROC_MOD}.launch_sync_subprocess", return_value=(MagicMock(), "test_id")
            ),
            patch(f"{_SYNC_PROC_MOD}.poll_sync_status_blocking", return_value=(True, {})),
            patch(f"{_SYNC_PROC_MOD}.cleanup_sync_status"),
            patch(f"{_HIST_MOD}.save_sync_history_entry") as mock_hist,
            patch(f"{_JOBS_MOD}._broadcast"),
            patch(f"{_JOBS_MOD}._notify"),
            patch(f"{_JOBS_MOD}._check_freshness"),
            patch(f"{_JOBS_MOD}._add_pending_dbt_source"),
            patch(f"{_JOBS_MOD}._run_coalesced_dbt"),
        ):
            from dango.platform.scheduling.jobs import run_scheduled_sync

            run_scheduled_sync("daily", ["src1", "src2"], project_root=str(tmp_path))

        assert mock_hist.call_count == 2

    def test_notifies_on_success(self, tmp_path):
        """Notification should be dispatched on successful sync."""
        config = _make_config_with_sources("src1")

        with (
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_CFG_MOD}.load_config", return_value=config),
            patch(
                f"{_SYNC_PROC_MOD}.launch_sync_subprocess", return_value=(MagicMock(), "test_id")
            ),
            patch(f"{_SYNC_PROC_MOD}.poll_sync_status_blocking", return_value=(True, {})),
            patch(f"{_SYNC_PROC_MOD}.cleanup_sync_status"),
            patch(f"{_HIST_MOD}.save_sync_history_entry"),
            patch(f"{_JOBS_MOD}._broadcast"),
            patch(f"{_JOBS_MOD}._notify") as mock_notify,
            patch(f"{_JOBS_MOD}._check_freshness"),
            patch(f"{_JOBS_MOD}._add_pending_dbt_source"),
            patch(f"{_JOBS_MOD}._run_coalesced_dbt"),
        ):
            from dango.platform.scheduling.jobs import run_scheduled_sync

            run_scheduled_sync("daily", ["src1"], project_root=str(tmp_path))

        mock_notify.assert_called_once()

    def test_no_sources_resolved_records_and_returns(self, tmp_path):
        """When all source names are unknown, record no_sources and return early."""
        config = MagicMock()
        config.sources.get_source.return_value = None  # all unknown

        with (
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_CFG_MOD}.load_config", return_value=config),
            patch(f"{_SYNC_PROC_MOD}.launch_sync_subprocess") as mock_launch,
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
            patch(f"{_JOBS_MOD}._log_execution_event") as mock_record,
        ):
            from dango.platform.scheduling.jobs import run_scheduled_sync

            run_scheduled_sync("daily", ["bogus"], project_root=str(tmp_path))

        mock_launch.assert_not_called()
        mock_bc.assert_not_called()
        mock_record.assert_called_once()
        assert mock_record.call_args[1]["status"] == "no_sources"

    def test_sync_only_skips_coalesced_dbt(self, tmp_path):
        """skip_dbt=True skips _add_pending_dbt_source and _run_coalesced_dbt."""
        config = _make_config_with_sources("src1")

        with (
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_CFG_MOD}.load_config", return_value=config),
            patch(
                f"{_SYNC_PROC_MOD}.launch_sync_subprocess",
                return_value=(MagicMock(), "test_id"),
            ),
            patch(f"{_SYNC_PROC_MOD}.poll_sync_status_blocking", return_value=(True, {})),
            patch(f"{_SYNC_PROC_MOD}.cleanup_sync_status"),
            patch(f"{_HIST_MOD}.save_sync_history_entry"),
            patch(f"{_JOBS_MOD}._broadcast"),
            patch(f"{_JOBS_MOD}._notify"),
            patch(f"{_JOBS_MOD}._check_freshness"),
            patch(f"{_JOBS_MOD}._add_pending_dbt_source") as mock_pending,
            patch(f"{_JOBS_MOD}._run_coalesced_dbt") as mock_dbt,
        ):
            from dango.platform.scheduling.jobs import run_scheduled_sync

            run_scheduled_sync("daily", ["src1"], project_root=str(tmp_path), skip_dbt=True)

        mock_pending.assert_not_called()
        mock_dbt.assert_not_called()

    def test_is_pickle_serializable(self):
        """Job function must be pickle-serializable (APScheduler requirement)."""
        import pickle

        from dango.platform.scheduling.jobs import run_scheduled_sync

        data = pickle.dumps(run_scheduled_sync)
        restored = pickle.loads(data)  # noqa: S301
        assert callable(restored)


# ---------------------------------------------------------------------------
# run_scheduled_dbt
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunScheduledDbt:
    """Test run_scheduled_dbt() job function."""

    def test_acquires_lock_and_calls_dbt(self, tmp_path):
        mock_lock = MagicMock()

        with (
            patch.object(_dbt_lock_module, "DbtLock", return_value=mock_lock),
            patch(f"{_DBT_MOD}.run_dbt_models", return_value=(True, "ok")),
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_JOBS_MOD}._broadcast"),
            patch(f"{_JOBS_MOD}._notify"),
        ):
            from dango.platform.scheduling.jobs import run_scheduled_dbt

            run_scheduled_dbt("nightly", "model_name+", project_root=str(tmp_path))

        mock_lock.acquire.assert_called_once()
        mock_lock.release.assert_called_once()

    def test_broadcasts_dbt_events(self, tmp_path):
        mock_lock = MagicMock()

        with (
            patch.object(_dbt_lock_module, "DbtLock", return_value=mock_lock),
            patch(f"{_DBT_MOD}.run_dbt_models", return_value=(True, "ok")),
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
            patch(f"{_JOBS_MOD}._notify"),
        ):
            from dango.platform.scheduling.jobs import run_scheduled_dbt

            run_scheduled_dbt("nightly", None, project_root=str(tmp_path))

        events = [c.args[0]["event"] for c in mock_bc.call_args_list]
        assert "dbt_started" in events
        assert "dbt_completed" in events

    def test_lock_failure_broadcasts_and_records(self, tmp_path):
        """DbtLockError should broadcast job_queued + dbt_failed and record lock_failed."""
        from dango.exceptions import DbtLockError

        mock_lock = MagicMock()
        mock_lock.acquire.side_effect = DbtLockError("busy")

        with (
            patch.object(_dbt_lock_module, "DbtLock", return_value=mock_lock),
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
            patch(f"{_JOBS_MOD}._notify") as mock_notify,
            patch(f"{_JOBS_MOD}._log_execution_event") as mock_record,
        ):
            from dango.platform.scheduling.jobs import run_scheduled_dbt

            run_scheduled_dbt("nightly", None, project_root=str(tmp_path))

        events = [c.args[0]["event"] for c in mock_bc.call_args_list]
        assert "job_queued" in events
        assert "dbt_failed" in events
        mock_notify.assert_called_once()
        mock_record.assert_called_once()
        assert mock_record.call_args[1]["status"] == "lock_failed"

    def test_exception_broadcasts_dbt_failed(self, tmp_path):
        """Unexpected exception from run_dbt_models should broadcast dbt_failed."""
        mock_lock = MagicMock()

        with (
            patch.object(_dbt_lock_module, "DbtLock", return_value=mock_lock),
            patch(f"{_DBT_MOD}.run_dbt_models", side_effect=RuntimeError("crash")),
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
            patch(f"{_JOBS_MOD}._notify"),
            patch(f"{_JOBS_MOD}._log_execution_event") as mock_record,
        ):
            from dango.platform.scheduling.jobs import run_scheduled_dbt

            run_scheduled_dbt("nightly", None, project_root=str(tmp_path))

        events = [c.args[0]["event"] for c in mock_bc.call_args_list]
        assert "dbt_failed" in events
        mock_record.assert_called()
        assert mock_record.call_args[1]["status"] == "failed"
        assert "crash" in mock_record.call_args[1]["error"]
        mock_lock.release.assert_called_once()

    def test_dbt_failure_notifies(self, tmp_path):
        mock_lock = MagicMock()

        with (
            patch.object(_dbt_lock_module, "DbtLock", return_value=mock_lock),
            patch(f"{_DBT_MOD}.run_dbt_models", return_value=(False, "dbt error")),
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
            patch(f"{_JOBS_MOD}._notify") as mock_notify,
        ):
            from dango.platform.scheduling.jobs import run_scheduled_dbt

            run_scheduled_dbt("nightly", None, project_root=str(tmp_path))

        events = [c.args[0]["event"] for c in mock_bc.call_args_list]
        assert "dbt_failed" in events
        mock_notify.assert_called_once()

    def test_is_pickle_serializable(self):
        """Job function must be pickle-serializable (APScheduler requirement)."""
        import pickle

        from dango.platform.scheduling.jobs import run_scheduled_dbt

        data = pickle.dumps(run_scheduled_dbt)
        restored = pickle.loads(data)  # noqa: S301
        assert callable(restored)


# ---------------------------------------------------------------------------
# _notify helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNotifyHelper:
    """Test _notify() never-raises contract."""

    def test_swallows_sender_exception(self):
        """_notify must never propagate exceptions from sender.send()."""
        import dango.platform.scheduling.jobs as jobs_mod

        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        old = jobs_mod._event_loop
        try:
            jobs_mod._event_loop = loop

            sender = MagicMock()
            sender.is_configured = True
            sender.send.side_effect = RuntimeError("webhook exploded")

            # Should not raise
            jobs_mod._notify(
                sender,
                event_type="TEST",
                schedule_name="daily",
            )
        finally:
            jobs_mod._event_loop = old

    def test_skips_when_sender_not_configured(self):
        """_notify should be a no-op when sender is not configured."""
        import dango.platform.scheduling.jobs as jobs_mod

        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        old = jobs_mod._event_loop
        try:
            jobs_mod._event_loop = loop

            sender = MagicMock()
            sender.is_configured = False

            with patch(f"{_JOBS_MOD}.asyncio") as mock_asyncio:
                jobs_mod._notify(sender, event_type="TEST", schedule_name="daily")

            mock_asyncio.run_coroutine_threadsafe.assert_not_called()
        finally:
            jobs_mod._event_loop = old

    def test_passes_rows_loaded_and_dashboard_url(self):
        """_notify passes rows_loaded and dashboard_url to sender.send()."""
        import dango.platform.scheduling.jobs as jobs_mod

        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        old = jobs_mod._event_loop
        try:
            jobs_mod._event_loop = loop

            sender = MagicMock()
            sender.is_configured = True

            jobs_mod._notify(
                sender,
                event_type="TEST",
                schedule_name="daily",
                rows_loaded=42,
                dashboard_url="https://example.com",
            )

            sender.send.assert_called_once()
            call_kwargs = sender.send.call_args[1]
            assert call_kwargs["rows_loaded"] == 42
            assert call_kwargs["dashboard_url"] == "https://example.com"
        finally:
            jobs_mod._event_loop = old

    def test_rows_loaded_zero_is_preserved(self):
        """_notify should pass rows_loaded=0, not convert it to None."""
        import dango.platform.scheduling.jobs as jobs_mod

        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        old = jobs_mod._event_loop
        try:
            jobs_mod._event_loop = loop

            sender = MagicMock()
            sender.is_configured = True

            jobs_mod._notify(
                sender,
                event_type="TEST",
                schedule_name="daily",
                rows_loaded=0,
            )

            call_kwargs = sender.send.call_args[1]
            assert call_kwargs["rows_loaded"] == 0
        finally:
            jobs_mod._event_loop = old


# ---------------------------------------------------------------------------
# _build_dashboard_url
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildDashboardUrl:
    """Test _build_dashboard_url() domain detection."""

    def test_returns_domain_when_cloud_config_has_domain(self, tmp_path):
        """Cloud domain takes precedence over localhost."""
        import dango.platform.scheduling.jobs as jobs_mod

        mock_loader = MagicMock()
        mock_cloud = MagicMock()
        mock_cloud.domain = "app.example.com"
        mock_loader.return_value.load_cloud_config.return_value = mock_cloud

        with patch("dango.config.ConfigLoader", mock_loader):
            url = jobs_mod._build_dashboard_url(tmp_path)

        assert url == "https://app.example.com"

    def test_returns_localhost_when_no_cloud_config(self, tmp_path):
        """Falls back to localhost when no cloud config."""
        import dango.platform.scheduling.jobs as jobs_mod

        mock_loader = MagicMock()
        mock_loader.return_value.load_cloud_config.return_value = None
        mock_config = MagicMock()
        mock_config.platform.port = 8800
        mock_loader.return_value.load_config.return_value = mock_config

        with patch("dango.config.ConfigLoader", mock_loader):
            url = jobs_mod._build_dashboard_url(tmp_path)

        assert url == "http://localhost:8800"

    def test_returns_none_on_error(self, tmp_path):
        """Returns None when config loading fails."""
        import dango.platform.scheduling.jobs as jobs_mod

        with patch("dango.config.ConfigLoader", side_effect=Exception("boom")):
            url = jobs_mod._build_dashboard_url(tmp_path)

        assert url is None


# ---------------------------------------------------------------------------
# _check_freshness
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckFreshness:
    """Test _check_freshness() staleness detection."""

    def test_stale_triggers_notification(self, tmp_path):
        from dango.platform.scheduling.jobs import _check_freshness

        old_ts = "2026-01-01T00:00:00"
        mock_sender = MagicMock()
        mock_sender.is_configured = True

        notif_config = MagicMock()
        notif_config.stale_threshold_hours = 24

        with (
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=notif_config),
            patch(f"{_HIST_MOD}.load_sync_history", return_value=[{"completed_at": old_ts}]),
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
            patch(f"{_JOBS_MOD}._notify") as mock_notify,
        ):
            _check_freshness(tmp_path, "daily", ["src1"], mock_sender)

        assert any(c.args[0].get("event") == "sync_stale" for c in mock_bc.call_args_list)
        mock_notify.assert_called_once()

    def test_fresh_data_no_notification(self, tmp_path):
        from datetime import datetime, timezone

        from dango.platform.scheduling.jobs import _check_freshness

        recent_ts = datetime.now(tz=timezone.utc).isoformat()
        mock_sender = MagicMock()
        mock_sender.is_configured = True

        notif_config = MagicMock()
        notif_config.stale_threshold_hours = 24

        with (
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=notif_config),
            patch(f"{_HIST_MOD}.load_sync_history", return_value=[{"completed_at": recent_ts}]),
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
            patch(f"{_JOBS_MOD}._notify") as mock_notify,
        ):
            _check_freshness(tmp_path, "daily", ["src1"], mock_sender)

        mock_bc.assert_not_called()
        mock_notify.assert_not_called()

    def test_no_config_is_noop(self, tmp_path):
        from dango.platform.scheduling.jobs import _check_freshness

        mock_sender = MagicMock()

        with patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None):
            _check_freshness(tmp_path, "daily", ["src1"], mock_sender)

    def test_missing_history_is_noop(self, tmp_path):
        from dango.platform.scheduling.jobs import _check_freshness

        mock_sender = MagicMock()
        mock_sender.is_configured = True

        notif_config = MagicMock()
        notif_config.stale_threshold_hours = 24

        with (
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=notif_config),
            patch(f"{_HIST_MOD}.load_sync_history", return_value=[]),
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
        ):
            _check_freshness(tmp_path, "daily", ["src1"], mock_sender)

        mock_bc.assert_not_called()
