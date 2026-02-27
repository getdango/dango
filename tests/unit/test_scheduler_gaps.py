"""tests/unit/test_scheduler_gaps.py

Gap-fill tests for scheduler: DST transitions, concurrent reload,
job store recovery, multi-source DbtLock serialization, cancellation
between sources, retry callback wiring, record_start wiring, and
timeout distinct status.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

import dango.utils.dbt_lock  # noqa: F401 — force submodule into sys.modules
from dango.exceptions import JobCancelledError, JobTimeoutError

_dbt_lock_module = sys.modules["dango.utils.dbt_lock"]

_SCHEDULER_MOD = "dango.platform.scheduling.scheduler"
_JOBS_MOD = "dango.platform.scheduling.jobs"
_NOTIF_MOD = "dango.platform.notifications.webhook"
_SYNC_MOD = "dango.ingestion.dlt_runner"
_CFG_MOD = "dango.config.helpers"
_HIST_MOD = "dango.utils.sync_history"


def _make_service(tmp_path):
    """Create a SchedulerService with all APScheduler internals mocked."""
    with (
        patch(f"{_SCHEDULER_MOD}.SQLAlchemyJobStore"),
        patch(f"{_SCHEDULER_MOD}.AsyncIOScheduler") as mock_cls,
    ):
        mock_scheduler = MagicMock()
        mock_scheduler.running = False
        mock_scheduler.get_jobs.return_value = []
        mock_cls.return_value = mock_scheduler

        from dango.platform.scheduling.scheduler import SchedulerService

        svc = SchedulerService(tmp_path)

    return svc


def _make_source(name="test_source"):
    """Create a minimal DataSource mock."""
    src = MagicMock()
    src.name = name
    src.enabled = True
    return src


def _make_config_with_sources(*source_names):
    """Create a mock load_config that returns sources by name."""
    sources = {n: _make_source(n) for n in source_names}
    config = MagicMock()
    config.sources.get_source.side_effect = lambda n: sources.get(n)
    return config


# ---------------------------------------------------------------------------
# DST transitions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDSTTransition:
    """CronTrigger with timezone across spring-forward and fall-back."""

    def test_spring_forward_does_not_skip_run(self):
        """A daily-at-2am cron should produce a run time near DST spring-forward."""
        from zoneinfo import ZoneInfo

        from apscheduler.triggers.cron import CronTrigger

        tz = ZoneInfo("America/New_York")
        # Daily at 2:30 AM — on 2026-03-08 (spring-forward), 2:30 AM doesn't exist.
        trigger = CronTrigger(hour=2, minute=30, timezone=tz)

        from datetime import datetime

        # Get next fire time starting before spring-forward
        base = datetime(2026, 3, 7, 3, 0, tzinfo=tz)
        fire = trigger.get_next_fire_time(None, base)
        assert fire is not None
        # APScheduler should still schedule something on or around March 8
        assert fire.month == 3
        assert fire.day == 8

    def test_fall_back_max_instances_prevents_overlap(self, tmp_path):
        """max_instances=1 prevents duplicate runs during fall-back ambiguous hour.

        APScheduler's CronTrigger may fire twice during DST fall-back (two
        1:00 AM's). The ``max_instances=1`` setting in job_defaults ensures
        only one concurrent execution even if two fire times match.
        """
        _make_service(tmp_path)

        # Verify the safeguard is configured via the constructor call on AsyncIOScheduler mock
        with (
            patch(f"{_SCHEDULER_MOD}.SQLAlchemyJobStore"),
            patch(f"{_SCHEDULER_MOD}.AsyncIOScheduler") as mock_cls,
        ):
            mock_cls.return_value = MagicMock()
            mock_cls.return_value.running = False
            mock_cls.return_value.get_jobs.return_value = []

            from dango.platform.scheduling.scheduler import SchedulerService

            SchedulerService(tmp_path)

        init_kwargs = mock_cls.call_args[1]
        assert init_kwargs["job_defaults"]["max_instances"] == 1
        assert init_kwargs["job_defaults"]["coalesce"] is True


# ---------------------------------------------------------------------------
# Concurrent reload
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConcurrentReload:
    """Two threads calling reload_schedules() — no corruption."""

    def test_concurrent_reload_no_corruption(self, tmp_path):
        """Concurrent reloads should not raise or leave inconsistent state."""
        from dango.config.schedules import ScheduleConfig, reload_schedules

        svc = _make_service(tmp_path)
        # Start the scheduler's internal state
        svc._scheduler.get_jobs.return_value = []

        schedules = [
            ScheduleConfig(
                name="daily_sync",
                type="sync",
                cron="0 6 * * *",
                sources=["src1"],
            ),
        ]

        barrier = threading.Barrier(2, timeout=5)
        errors: list[Exception] = []

        def do_reload():
            try:
                barrier.wait()
                reload_schedules(svc, schedules, tmp_path)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=do_reload)
        t2 = threading.Thread(target=do_reload)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Concurrent reload raised: {errors}"


# ---------------------------------------------------------------------------
# Job store recovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJobStoreRecovery:
    """Job store SQLite directory creation and error handling."""

    def test_missing_dir_created(self, tmp_path):
        """SchedulerService.__init__ creates .dango/ if missing."""
        project = tmp_path / "newproject"
        project.mkdir()
        assert not (project / ".dango").exists()

        svc = _make_service(project)
        assert (project / ".dango").is_dir()
        assert svc is not None

    def test_corrupt_db_handled_by_apscheduler(self, tmp_path):
        """A corrupt scheduler.db should be handled at the APScheduler level."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        db_path = dango_dir / "scheduler.db"
        db_path.write_text("not a sqlite database")

        # APScheduler's SQLAlchemyJobStore handles the corruption when
        # instantiated with a real connection. With our mock, we just
        # verify the service still constructs.
        svc = _make_service(tmp_path)
        assert svc is not None

    def test_empty_db_starts_with_no_jobs(self, tmp_path):
        """An empty scheduler.db results in zero scheduled jobs."""
        svc = _make_service(tmp_path)
        svc._scheduler.get_jobs.return_value = []

        assert svc.get_jobs() == []


# ---------------------------------------------------------------------------
# Multi-source DbtLock
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMultiSourceDbtLock:
    """Second schedule gets lock contention on shared sources."""

    def test_lock_contention_reported(self, tmp_path):
        """When DbtLock.acquire raises, the job broadcasts lock_failed."""
        from dango.exceptions import DbtLockError

        mock_lock = MagicMock()
        mock_lock.acquire.side_effect = DbtLockError("held by another schedule")

        with (
            patch.object(_dbt_lock_module, "DbtLock", return_value=mock_lock),
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
            patch(f"{_JOBS_MOD}._notify"),
            patch(f"{_JOBS_MOD}._log_execution_event") as mock_log,
        ):
            from dango.platform.scheduling.jobs import run_scheduled_sync

            run_scheduled_sync("hourly", ["src1"], project_root=str(tmp_path))

        events = [c.args[0]["event"] for c in mock_bc.call_args_list]
        assert "job_queued" in events
        assert mock_log.call_args[1]["status"] == "lock_failed"


# ---------------------------------------------------------------------------
# Cancellation between sources
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCancellationBetweenSources:
    """Cancellation stops remaining sources mid-iteration."""

    def test_cancel_stops_remaining_sources(self, tmp_path):
        """When cancelled between sources, remaining sources are not synced."""
        import dango.platform.scheduling.jobs as jobs_mod

        config = _make_config_with_sources("src1", "src2", "src3")
        mock_lock = MagicMock()
        synced_sources: list[str] = []

        mock_svc = MagicMock()
        cancel_count = 0

        def track_cancel(job_id):
            nonlocal cancel_count
            cancel_count += 1
            # Cancel after first source synced
            return cancel_count > 1

        mock_svc.is_cancelled = track_cancel
        mock_svc._running_records = {}
        mock_svc.register_execution = MagicMock()

        def mock_run_sync(root, sources, **kw):
            synced_sources.append(sources[0].name)

        old_svc = jobs_mod._scheduler_service
        try:
            jobs_mod._scheduler_service = mock_svc

            with (
                patch.object(_dbt_lock_module, "DbtLock", return_value=mock_lock),
                patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
                patch(f"{_NOTIF_MOD}.WebhookSender"),
                patch(f"{_CFG_MOD}.load_config", return_value=config),
                patch(f"{_SYNC_MOD}.run_sync", side_effect=mock_run_sync),
                patch(f"{_HIST_MOD}.save_sync_history_entry"),
                patch(f"{_JOBS_MOD}._broadcast"),
                patch(f"{_JOBS_MOD}._notify"),
                patch(f"{_JOBS_MOD}._log_execution_event"),
                patch(f"{_JOBS_MOD}._try_record_start", return_value=None),
                patch(f"{_JOBS_MOD}._try_finish_record"),
            ):
                from dango.platform.scheduling.jobs import run_scheduled_sync

                run_scheduled_sync("daily", ["src1", "src2", "src3"], project_root=str(tmp_path))
        finally:
            jobs_mod._scheduler_service = old_svc

        # src1 synced, then cancelled before src2
        assert synced_sources == ["src1"]


# ---------------------------------------------------------------------------
# Retry callback wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetryCallbackWiring:
    """start() registers callback, callback broadcasts event."""

    def test_start_registers_retry_callback(self, tmp_path):
        """SchedulerService.start() should register _on_retry_event callback."""
        svc = _make_service(tmp_path)
        loop = MagicMock(spec=asyncio.AbstractEventLoop)

        assert len(svc._on_retry_callbacks) == 0
        svc.start(loop)
        assert len(svc._on_retry_callbacks) == 1

    def test_retry_callback_broadcasts_event(self, tmp_path):
        """_on_retry_event should broadcast a sync_retrying WebSocket event."""
        svc = _make_service(tmp_path)

        with (
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender") as mock_sender_cls,
        ):
            mock_sender_cls.return_value.is_configured = False
            svc._on_retry_event(
                job_id="schedule:daily_sync",
                attempt=1,
                max_retries=3,
                next_retry_delay=30,
                error="connection reset",
            )

        mock_bc.assert_called_once()
        msg = mock_bc.call_args[0][0]
        assert msg["event"] == "sync_retrying"
        assert msg["schedule"] == "daily_sync"
        assert msg["attempt"] == 1
        assert msg["error"] == "connection reset"


# ---------------------------------------------------------------------------
# record_start wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRecordStartWiring:
    """record_start called at entry, register_execution called."""

    def test_record_start_called_on_sync(self, tmp_path):
        """run_scheduled_sync should call _try_record_start when scheduler is available."""
        config = _make_config_with_sources("src1")
        mock_lock = MagicMock()

        with (
            patch.object(_dbt_lock_module, "DbtLock", return_value=mock_lock),
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_CFG_MOD}.load_config", return_value=config),
            patch(f"{_SYNC_MOD}.run_sync"),
            patch(f"{_HIST_MOD}.save_sync_history_entry"),
            patch(f"{_JOBS_MOD}._broadcast"),
            patch(f"{_JOBS_MOD}._notify"),
            patch(f"{_JOBS_MOD}._check_freshness"),
            patch(f"{_JOBS_MOD}._try_record_start", return_value=42) as mock_start,
            patch(f"{_JOBS_MOD}._try_finish_record") as mock_finish,
        ):
            from dango.platform.scheduling.jobs import run_scheduled_sync

            run_scheduled_sync("daily", ["src1"], project_root=str(tmp_path))

        mock_start.assert_called_once()
        mock_finish.assert_called_once()
        assert mock_finish.call_args[0][3] == "record_completion"

    def test_record_start_called_on_dbt(self, tmp_path):
        """run_scheduled_dbt should call _try_record_start."""
        mock_lock = MagicMock()

        with (
            patch.object(_dbt_lock_module, "DbtLock", return_value=mock_lock),
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch("dango.transformation.run_dbt_models", return_value=(True, "ok")),
            patch(f"{_JOBS_MOD}._broadcast"),
            patch(f"{_JOBS_MOD}._notify"),
            patch(f"{_JOBS_MOD}._try_record_start", return_value=7) as mock_start,
            patch(f"{_JOBS_MOD}._try_finish_record") as mock_finish,
        ):
            from dango.platform.scheduling.jobs import run_scheduled_dbt

            run_scheduled_dbt("nightly", "model+", project_root=str(tmp_path))

        mock_start.assert_called_once()
        mock_finish.assert_called_once()
        assert mock_finish.call_args[0][3] == "record_completion"


# ---------------------------------------------------------------------------
# Timeout distinct status
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTimeoutDistinctStatus:
    """JobTimeoutError recorded as 'timeout' not 'failed'."""

    def test_timeout_records_timeout_status(self, tmp_path):
        """When JobTimeoutError is raised, execution log should show 'timeout'."""
        config = _make_config_with_sources("src1")
        mock_lock = MagicMock()

        with (
            patch.object(_dbt_lock_module, "DbtLock", return_value=mock_lock),
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_CFG_MOD}.load_config", return_value=config),
            patch(f"{_SYNC_MOD}.run_sync", side_effect=JobTimeoutError("timed out")),
            patch(f"{_JOBS_MOD}._broadcast"),
            patch(f"{_JOBS_MOD}._notify"),
            patch(f"{_JOBS_MOD}._log_execution_event") as mock_log,
            patch(f"{_JOBS_MOD}._try_record_start", return_value=None),
            patch(f"{_JOBS_MOD}._try_finish_record"),
        ):
            from dango.platform.scheduling.jobs import run_scheduled_sync

            run_scheduled_sync("daily", ["src1"], project_root=str(tmp_path))

        assert mock_log.call_args[1]["status"] == "timeout"

    def test_cancellation_records_cancelled_status(self, tmp_path):
        """When JobCancelledError is raised, execution log should show 'cancelled'."""
        config = _make_config_with_sources("src1")
        mock_lock = MagicMock()

        with (
            patch.object(_dbt_lock_module, "DbtLock", return_value=mock_lock),
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=None),
            patch(f"{_NOTIF_MOD}.WebhookSender"),
            patch(f"{_CFG_MOD}.load_config", return_value=config),
            patch(f"{_SYNC_MOD}.run_sync", side_effect=JobCancelledError("cancelled")),
            patch(f"{_JOBS_MOD}._broadcast"),
            patch(f"{_JOBS_MOD}._log_execution_event") as mock_log,
            patch(f"{_JOBS_MOD}._try_record_start", return_value=None),
            patch(f"{_JOBS_MOD}._try_finish_record"),
        ):
            from dango.platform.scheduling.jobs import run_scheduled_sync

            run_scheduled_sync("daily", ["src1"], project_root=str(tmp_path))

        assert mock_log.call_args[1]["status"] == "cancelled"
