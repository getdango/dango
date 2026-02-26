"""tests/unit/test_scheduler.py

Tests for dango.platform.scheduling — SchedulerService and module-level job stubs.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

# Patch both SQLAlchemyJobStore AND AsyncIOScheduler to avoid real
# APScheduler initialization (it validates isinstance on job stores).
_SCHEDULER_MOD = "dango.platform.scheduling.scheduler"


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


@pytest.mark.unit
class TestSchedulerServiceInit:
    """Test SchedulerService construction and configuration."""

    def test_init_configures_job_store_path(self, tmp_path):
        """Job store URL should point to .dango/scheduler.db."""
        with (
            patch(f"{_SCHEDULER_MOD}.SQLAlchemyJobStore") as mock_store,
            patch(f"{_SCHEDULER_MOD}.AsyncIOScheduler"),
        ):
            from dango.platform.scheduling.scheduler import SchedulerService

            SchedulerService(tmp_path)

        expected_url = f"sqlite:///{tmp_path / '.dango' / 'scheduler.db'}"
        mock_store.assert_called_once_with(url=expected_url)

    def test_ensures_dango_dir_exists(self, tmp_path):
        """.dango/ directory should be created if it doesn't exist."""
        project_root = tmp_path / "myproject"
        project_root.mkdir()

        with (
            patch(f"{_SCHEDULER_MOD}.SQLAlchemyJobStore"),
            patch(f"{_SCHEDULER_MOD}.AsyncIOScheduler"),
        ):
            from dango.platform.scheduling.scheduler import SchedulerService

            SchedulerService(project_root)

        assert (project_root / ".dango").is_dir()


@pytest.mark.unit
class TestSchedulerServiceLifecycle:
    """Test start/shutdown behaviour."""

    def test_start_calls_scheduler_start(self, tmp_path):
        """start() should call the underlying scheduler.start()."""
        svc = _make_service(tmp_path)
        loop = MagicMock(spec=asyncio.AbstractEventLoop)

        svc.start(loop)

        svc._scheduler.start.assert_called_once()

    def test_start_registers_event_listeners(self, tmp_path):
        """start() should register executed, error, and missed listeners."""
        svc = _make_service(tmp_path)
        loop = MagicMock(spec=asyncio.AbstractEventLoop)

        svc.start(loop)

        assert svc._scheduler.add_listener.call_count == 3

    def test_start_stores_loop(self, tmp_path):
        """start() should store the event loop for coroutine bridging."""
        svc = _make_service(tmp_path)
        loop = MagicMock(spec=asyncio.AbstractEventLoop)

        svc.start(loop)

        assert svc._loop is loop

    def test_shutdown_waits_by_default(self, tmp_path):
        """shutdown() should call scheduler.shutdown(wait=True) by default."""
        svc = _make_service(tmp_path)
        svc._scheduler.running = True

        svc.shutdown()

        svc._scheduler.shutdown.assert_called_once_with(wait=True)

    def test_shutdown_no_wait(self, tmp_path):
        """shutdown(wait=False) should not wait for running jobs."""
        svc = _make_service(tmp_path)
        svc._scheduler.running = True

        svc.shutdown(wait=False)

        svc._scheduler.shutdown.assert_called_once_with(wait=False)

    def test_shutdown_skips_if_not_running(self, tmp_path):
        """shutdown() should be a no-op if the scheduler isn't running."""
        svc = _make_service(tmp_path)
        svc._scheduler.running = False

        svc.shutdown()

        svc._scheduler.shutdown.assert_not_called()

    def test_start_is_idempotent(self, tmp_path):
        """Calling start() twice should be a no-op the second time."""
        svc = _make_service(tmp_path)
        loop = MagicMock(spec=asyncio.AbstractEventLoop)

        svc.start(loop)
        svc.start(loop)

        # Listeners registered exactly once (3 listeners), not 6
        assert svc._scheduler.add_listener.call_count == 3
        svc._scheduler.start.assert_called_once()


@pytest.mark.unit
class TestSchedulerServiceJobs:
    """Test job management delegation."""

    def test_add_job_delegates(self, tmp_path):
        """add_job() should delegate to the underlying scheduler."""
        svc = _make_service(tmp_path)
        svc._scheduler.running = True
        func = MagicMock()
        trigger = "interval"

        svc.add_job(func, trigger, seconds=60, id="test-job")

        svc._scheduler.add_job.assert_called_once_with(func, trigger, seconds=60, id="test-job")

    def test_remove_job_delegates(self, tmp_path):
        """remove_job() should delegate to the underlying scheduler."""
        svc = _make_service(tmp_path)

        svc.remove_job("test-job")

        svc._scheduler.remove_job.assert_called_once_with("test-job")

    def test_get_jobs_returns_list(self, tmp_path):
        """get_jobs() should return the job list from the scheduler."""
        svc = _make_service(tmp_path)
        mock_jobs = [MagicMock(), MagicMock()]
        svc._scheduler.get_jobs.return_value = mock_jobs

        result = svc.get_jobs()

        assert result == mock_jobs


@pytest.mark.unit
class TestSchedulerServiceStatus:
    """Test get_status() output."""

    def test_get_status_when_running(self, tmp_path):
        """get_status() should report running=True with job count."""
        svc = _make_service(tmp_path)
        svc._scheduler.running = True

        job = MagicMock()
        job.next_run_time = None
        svc._scheduler.get_jobs.return_value = [job]

        status = svc.get_status()

        assert status["running"] is True
        assert status["job_count"] == 1
        assert status["next_run_time"] is None

    def test_get_status_when_stopped(self, tmp_path):
        """get_status() should report running=False when stopped."""
        svc = _make_service(tmp_path)
        svc._scheduler.running = False

        status = svc.get_status()

        assert status["running"] is False
        assert status["job_count"] == 0
        assert status["next_run_time"] is None

    def test_get_status_with_next_run_time(self, tmp_path):
        """get_status() should report the earliest next_run_time."""
        from datetime import datetime, timezone

        svc = _make_service(tmp_path)
        svc._scheduler.running = True

        job1 = MagicMock()
        job1.next_run_time = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        job2 = MagicMock()
        job2.next_run_time = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
        svc._scheduler.get_jobs.return_value = [job1, job2]

        status = svc.get_status()

        assert status["next_run_time"] == job2.next_run_time.isoformat()


@pytest.mark.unit
class TestSchedulerServiceEventListeners:
    """Test event listener logging."""

    def test_event_listener_job_executed_logs(self, tmp_path):
        """_on_job_executed should log at info level."""
        svc = _make_service(tmp_path)
        event = MagicMock()
        event.job_id = "test-job"
        event.scheduled_run_time = "2026-03-01T10:00:00"

        with patch(f"{_SCHEDULER_MOD}.logger") as mock_logger:
            svc._on_job_executed(event)

        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        assert call_args[0][0] == "scheduler_job_executed"
        assert call_args[1]["job_id"] == "test-job"

    def test_event_listener_job_error_logs(self, tmp_path):
        """_on_job_error should log at error level."""
        svc = _make_service(tmp_path)
        event = MagicMock()
        event.job_id = "test-job"
        event.scheduled_run_time = "2026-03-01T10:00:00"
        event.exception = ValueError("boom")
        event.traceback = "traceback here"

        with patch(f"{_SCHEDULER_MOD}.logger") as mock_logger:
            svc._on_job_error(event)

        mock_logger.error.assert_called_once()
        call_args = mock_logger.error.call_args
        assert call_args[0][0] == "scheduler_job_error"

    def test_event_listener_job_missed_logs(self, tmp_path):
        """_on_job_missed should log at warning level."""
        svc = _make_service(tmp_path)
        event = MagicMock()
        event.job_id = "test-job"
        event.scheduled_run_time = "2026-03-01T10:00:00"

        with patch(f"{_SCHEDULER_MOD}.logger") as mock_logger:
            svc._on_job_missed(event)

        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "scheduler_job_missed"


@pytest.mark.unit
class TestSchedulerServiceCoroutineBridge:
    """Test run_coroutine() async bridge."""

    def test_run_coroutine_bridge(self, tmp_path):
        """run_coroutine() should call asyncio.run_coroutine_threadsafe."""
        svc = _make_service(tmp_path)
        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        svc._loop = loop

        coro = MagicMock()

        with patch(f"{_SCHEDULER_MOD}.asyncio") as mock_asyncio:
            svc.run_coroutine(coro)

        mock_asyncio.run_coroutine_threadsafe.assert_called_once_with(coro, loop)

    def test_run_coroutine_raises_without_loop(self, tmp_path):
        """run_coroutine() should raise RuntimeError if not started."""
        svc = _make_service(tmp_path)

        with pytest.raises(RuntimeError, match="no event loop"):
            svc.run_coroutine(MagicMock())


@pytest.mark.unit
class TestSchedulerServiceCloudWarning:
    """Test dual-scheduler cloud detection."""

    def test_cloud_warning_logged(self, tmp_path):
        """Warning should be logged if cloud config has a droplet_id."""
        svc = _make_service(tmp_path)

        cloud_cfg = MagicMock()
        cloud_cfg.droplet_id = 12345

        mock_loader = MagicMock()
        mock_loader.return_value.load_cloud_config.return_value = cloud_cfg

        with (
            patch("dango.config.loader.ConfigLoader", mock_loader),
            patch(f"{_SCHEDULER_MOD}.logger") as mock_logger,
        ):
            svc._check_dual_scheduler()

        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "dual_scheduler_warning"

    def test_no_cloud_warning_when_no_deployment(self, tmp_path):
        """No warning when cloud config has no droplet_id."""
        svc = _make_service(tmp_path)

        cloud_cfg = MagicMock()
        cloud_cfg.droplet_id = None

        mock_loader = MagicMock()
        mock_loader.return_value.load_cloud_config.return_value = cloud_cfg

        with (
            patch("dango.config.loader.ConfigLoader", mock_loader),
            patch(f"{_SCHEDULER_MOD}.logger") as mock_logger,
        ):
            svc._check_dual_scheduler()

        mock_logger.warning.assert_not_called()

    def test_no_cloud_warning_when_no_config(self, tmp_path):
        """No warning when cloud config doesn't exist."""
        svc = _make_service(tmp_path)

        mock_loader = MagicMock()
        mock_loader.return_value.load_cloud_config.return_value = None

        with (
            patch("dango.config.loader.ConfigLoader", mock_loader),
            patch(f"{_SCHEDULER_MOD}.logger") as mock_logger,
        ):
            svc._check_dual_scheduler()

        mock_logger.warning.assert_not_called()


@pytest.mark.unit
class TestSchedulerStartupSummary:
    """Test startup summary logging."""

    def test_startup_summary_logged(self, tmp_path):
        """Startup summary should log job count and next run times."""
        svc = _make_service(tmp_path)

        job = MagicMock()
        job.id = "sync-google-sheets"
        job.next_run_time = MagicMock()
        job.next_run_time.__str__ = lambda self: "2026-03-01 10:00:00+00:00"
        svc._scheduler.get_jobs.return_value = [job]

        with patch(f"{_SCHEDULER_MOD}.logger") as mock_logger:
            svc._log_startup_summary()

        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        assert call_args[0][0] == "scheduler_started"
        assert call_args[1]["job_count"] == 1


@pytest.mark.unit
class TestJobFunctions:
    """Test module-level job functions are importable."""

    def test_run_scheduled_sync_is_module_level(self):
        """run_scheduled_sync should be importable at module level."""
        from dango.platform.scheduling.jobs import run_scheduled_sync

        assert callable(run_scheduled_sync)

    def test_run_scheduled_dbt_is_module_level(self):
        """run_scheduled_dbt should be importable at module level."""
        from dango.platform.scheduling.jobs import run_scheduled_dbt

        assert callable(run_scheduled_dbt)

    def test_configure_jobs_is_module_level(self):
        """configure_jobs should be importable at module level."""
        from dango.platform.scheduling.jobs import configure_jobs

        assert callable(configure_jobs)


_HEALTH_MOD = "dango.web.routes.health"


@pytest.mark.unit
class TestGetSchedulerStatus:
    """Test _get_scheduler_status() in health.py."""

    def test_returns_status_from_scheduler(self):
        """Should return scheduler.get_status() when scheduler is present."""
        from dango.web.routes.health import _get_scheduler_status

        mock_scheduler = MagicMock()
        mock_scheduler.get_status.return_value = {
            "running": True,
            "job_count": 3,
            "next_run_time": "2026-03-01T10:00:00+00:00",
        }

        # Patch at source — the lazy import does `from dango.web.app import app`
        from dango.web.app import app

        original_scheduler = getattr(app.state, "scheduler", None)
        app.state.scheduler = mock_scheduler
        try:
            result = _get_scheduler_status()
        finally:
            app.state.scheduler = original_scheduler

        assert result["running"] is True
        assert result["job_count"] == 3

    def test_returns_fallback_when_scheduler_is_none(self):
        """Should return fallback dict when scheduler is None."""
        from dango.web.app import app
        from dango.web.routes.health import _get_scheduler_status

        original_scheduler = getattr(app.state, "scheduler", None)
        app.state.scheduler = None
        try:
            result = _get_scheduler_status()
        finally:
            app.state.scheduler = original_scheduler

        assert result == {"running": False, "job_count": 0, "next_run_time": None}

    def test_returns_fallback_on_exception(self):
        """Should return fallback dict when an exception occurs."""
        from dango.web.routes.health import _get_scheduler_status

        mock_scheduler = MagicMock()
        mock_scheduler.get_status.side_effect = RuntimeError("boom")

        from dango.web.app import app

        original_scheduler = getattr(app.state, "scheduler", None)
        app.state.scheduler = mock_scheduler
        try:
            result = _get_scheduler_status()
        finally:
            app.state.scheduler = original_scheduler

        assert result == {"running": False, "job_count": 0, "next_run_time": None}

    def test_fallback_returns_fresh_dict_each_time(self):
        """Each fallback call should return an independent dict (no mutation)."""
        from dango.web.app import app
        from dango.web.routes.health import _get_scheduler_status

        original_scheduler = getattr(app.state, "scheduler", None)
        app.state.scheduler = None
        try:
            result1 = _get_scheduler_status()
            result1["running"] = True  # mutate
            result2 = _get_scheduler_status()
        finally:
            app.state.scheduler = original_scheduler

        assert result2["running"] is False
