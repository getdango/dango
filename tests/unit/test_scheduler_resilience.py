"""tests/unit/test_scheduler_resilience.py

Tests for scheduler resilience: retry, timeout, cancellation, and callbacks.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import JobCancelledError, JobTimeoutError

_SCHEDULER_MOD = "dango.platform.scheduling.scheduler"
_RESILIENCE_MOD = "dango.platform.scheduling.resilience"


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
class TestResilienceConfig:
    """Test ResilienceConfig default and custom values."""

    def test_default_values(self):
        from dango.platform.scheduling.resilience import ResilienceConfig

        cfg = ResilienceConfig()
        assert cfg.max_retries == 3
        assert cfg.retry_delays == (30, 120, 300)
        assert cfg.timeout_minutes == 60

    def test_custom_values(self):
        from dango.platform.scheduling.resilience import ResilienceConfig

        cfg = ResilienceConfig(max_retries=5, retry_delays=(10, 20), timeout_minutes=30)
        assert cfg.max_retries == 5
        assert cfg.retry_delays == (10, 20)
        assert cfg.timeout_minutes == 30

    def test_frozen(self):
        from dango.platform.scheduling.resilience import ResilienceConfig

        cfg = ResilienceConfig()
        with pytest.raises(AttributeError):
            cfg.max_retries = 10  # type: ignore[misc]

    def test_rejects_zero_max_retries(self):
        from dango.platform.scheduling.resilience import ResilienceConfig

        with pytest.raises(ValueError, match="max_retries must be at least 1"):
            ResilienceConfig(max_retries=0)

    def test_rejects_zero_timeout(self):
        from dango.platform.scheduling.resilience import ResilienceConfig

        with pytest.raises(ValueError, match="timeout_minutes must be positive"):
            ResilienceConfig(timeout_minutes=0)

    def test_rejects_negative_timeout(self):
        from dango.platform.scheduling.resilience import ResilienceConfig

        with pytest.raises(ValueError, match="timeout_minutes must be positive"):
            ResilienceConfig(timeout_minutes=-1)

    def test_rejects_empty_retry_delays(self):
        from dango.platform.scheduling.resilience import ResilienceConfig

        with pytest.raises(ValueError, match="retry_delays must not be empty"):
            ResilienceConfig(retry_delays=())


@pytest.mark.unit
class TestCancellation:
    """Test cancel flag management on SchedulerService."""

    def test_cancel_job_sets_flag(self, tmp_path):
        svc = _make_service(tmp_path)
        flag = svc._register_cancel_flag("job-1")
        assert svc.cancel_job("job-1") is True
        assert flag.is_set()

    def test_cancel_job_returns_false_for_unknown(self, tmp_path):
        svc = _make_service(tmp_path)
        assert svc.cancel_job("nonexistent") is False

    def test_is_cancelled_true_when_set(self, tmp_path):
        svc = _make_service(tmp_path)
        svc._register_cancel_flag("job-1").set()
        assert svc.is_cancelled("job-1") is True

    def test_is_cancelled_false_when_not_set_or_unknown(self, tmp_path):
        svc = _make_service(tmp_path)
        svc._register_cancel_flag("job-1")
        assert svc.is_cancelled("job-1") is False
        assert svc.is_cancelled("nonexistent") is False

    def test_clear_cancel_flag_removes(self, tmp_path):
        svc = _make_service(tmp_path)
        svc._register_cancel_flag("job-1")
        svc._clear_cancel_flag("job-1")
        assert "job-1" not in svc._cancel_flags

    def test_clear_cancel_flag_noop_for_unknown(self, tmp_path):
        _make_service(tmp_path)._clear_cancel_flag("nonexistent")

    def test_register_cancel_flag_warns_on_overwrite(self, tmp_path):
        svc = _make_service(tmp_path)
        svc._register_cancel_flag("job-1")

        with patch(f"{_SCHEDULER_MOD}.logger") as mock_logger:
            svc._register_cancel_flag("job-1")

        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][0] == "cancel_flag_overwrite"

    def test_shutdown_sets_all_flags(self, tmp_path):
        svc = _make_service(tmp_path)
        svc._scheduler.running = True
        flag1 = svc._register_cancel_flag("job-1")
        flag2 = svc._register_cancel_flag("job-2")

        svc.shutdown()

        assert flag1.is_set()
        assert flag2.is_set()
        assert len(svc._cancel_flags) == 0


@pytest.mark.unit
class TestRunWithResilience:
    """Test the run_with_resilience() wrapper."""

    def test_success_passthrough(self, tmp_path):
        from dango.platform.scheduling.resilience import ResilienceConfig, run_with_resilience

        svc = _make_service(tmp_path)
        func = MagicMock(return_value="result")

        result = run_with_resilience(
            func,
            "arg1",
            scheduler_service=svc,
            job_id="job-1",
            resilience=ResilienceConfig(timeout_minutes=1),
            key="val",
        )

        assert result == "result"
        func.assert_called_once_with("arg1", key="val")

    def test_cancel_flag_cleared_on_success(self, tmp_path):
        from dango.platform.scheduling.resilience import ResilienceConfig, run_with_resilience

        svc = _make_service(tmp_path)
        func = MagicMock(return_value="ok")

        run_with_resilience(
            func,
            scheduler_service=svc,
            job_id="job-1",
            resilience=ResilienceConfig(timeout_minutes=1),
        )

        assert "job-1" not in svc._cancel_flags

    def test_cancel_flag_cleared_on_failure(self, tmp_path):
        from dango.platform.scheduling.resilience import ResilienceConfig, run_with_resilience

        svc = _make_service(tmp_path)
        func = MagicMock(side_effect=ValueError("boom"))

        with pytest.raises(ValueError, match="boom"):
            run_with_resilience(
                func,
                scheduler_service=svc,
                job_id="job-1",
                resilience=ResilienceConfig(max_retries=1, timeout_minutes=1),
            )

        assert "job-1" not in svc._cancel_flags

    def test_retry_on_failure(self, tmp_path):
        from dango.platform.scheduling.resilience import ResilienceConfig, run_with_resilience

        svc = _make_service(tmp_path)
        func = MagicMock(side_effect=[ValueError("fail1"), "success"])

        result = run_with_resilience(
            func,
            scheduler_service=svc,
            job_id="job-1",
            resilience=ResilienceConfig(
                max_retries=2,
                retry_delays=(0,),
                timeout_minutes=1,
            ),
        )

        assert result == "success"
        assert func.call_count == 2

    def test_all_retries_exhausted(self, tmp_path):
        from dango.platform.scheduling.resilience import ResilienceConfig, run_with_resilience

        svc = _make_service(tmp_path)
        func = MagicMock(side_effect=ValueError("persistent"))

        with pytest.raises(ValueError, match="persistent"):
            run_with_resilience(
                func,
                scheduler_service=svc,
                job_id="job-1",
                resilience=ResilienceConfig(
                    max_retries=2,
                    retry_delays=(0,),
                    timeout_minutes=1,
                ),
            )

        assert func.call_count == 2

    def test_retry_emits_callback(self, tmp_path):
        from dango.platform.scheduling.resilience import ResilienceConfig, run_with_resilience

        svc = _make_service(tmp_path)
        callback = MagicMock()
        svc._on_retry_callbacks.append(callback)
        func = MagicMock(side_effect=[ValueError("fail"), "ok"])

        run_with_resilience(
            func,
            scheduler_service=svc,
            job_id="job-1",
            resilience=ResilienceConfig(
                max_retries=2,
                retry_delays=(0,),
                timeout_minutes=1,
            ),
        )

        callback.assert_called_once()
        call_kwargs = callback.call_args[1]
        assert call_kwargs["job_id"] == "job-1"
        assert call_kwargs["attempt"] == 1
        assert call_kwargs["max_retries"] == 2
        assert call_kwargs["next_retry_delay"] == 0
        assert "fail" in call_kwargs["error"]

    def test_cancel_before_execution(self, tmp_path):
        from dango.platform.scheduling.resilience import ResilienceConfig, run_with_resilience

        svc = _make_service(tmp_path)
        func = MagicMock(return_value="ok")

        # Patch _register_cancel_flag to return an already-set event
        already_set = threading.Event()
        already_set.set()
        svc._register_cancel_flag = MagicMock(return_value=already_set)

        with pytest.raises(JobCancelledError):
            run_with_resilience(
                func,
                scheduler_service=svc,
                job_id="job-1",
                resilience=ResilienceConfig(timeout_minutes=1),
            )

        func.assert_not_called()

    def test_cancel_during_retry_wait(self, tmp_path):
        from dango.platform.scheduling.resilience import ResilienceConfig, run_with_resilience

        svc = _make_service(tmp_path)
        func = MagicMock(side_effect=ValueError("fail"))

        # After the first failure, set the cancel flag during the retry wait.
        # cancel_flag.wait() returns True when the flag is set.
        original_register = svc._register_cancel_flag

        def register_and_schedule_cancel(job_id):
            flag = original_register(job_id)
            # Set flag after a tiny delay to simulate cancel during wait
            timer = threading.Timer(0.01, flag.set)
            timer.start()
            return flag

        svc._register_cancel_flag = register_and_schedule_cancel

        with pytest.raises(JobCancelledError, match="cancelled during retry wait"):
            run_with_resilience(
                func,
                scheduler_service=svc,
                job_id="job-1",
                resilience=ResilienceConfig(
                    max_retries=3,
                    retry_delays=(60,),
                    timeout_minutes=1,
                ),
            )

    def test_uses_default_config_when_none(self, tmp_path):
        from dango.platform.scheduling.resilience import run_with_resilience

        svc = _make_service(tmp_path)
        func = MagicMock(return_value="ok")

        result = run_with_resilience(
            func,
            scheduler_service=svc,
            job_id="job-1",
            resilience=None,
        )

        assert result == "ok"

    def test_timeout_does_not_retry(self, tmp_path):
        from dango.platform.scheduling.resilience import ResilienceConfig, run_with_resilience

        svc = _make_service(tmp_path)

        def slow_func():
            raise JobTimeoutError("timed out")

        with pytest.raises(JobTimeoutError):
            run_with_resilience(
                slow_func,
                scheduler_service=svc,
                job_id="job-1",
                resilience=ResilienceConfig(max_retries=3, timeout_minutes=1),
            )

    def test_timeout_emits_callback(self, tmp_path):
        from dango.platform.scheduling.resilience import ResilienceConfig, run_with_resilience

        svc = _make_service(tmp_path)
        callback = MagicMock()
        svc._on_timeout_callbacks.append(callback)

        def slow_func():
            raise JobTimeoutError("timed out")

        with pytest.raises(JobTimeoutError):
            run_with_resilience(
                slow_func,
                scheduler_service=svc,
                job_id="job-1",
                resilience=ResilienceConfig(max_retries=3, timeout_minutes=5),
            )

        callback.assert_called_once()
        call_kwargs = callback.call_args[1]
        assert call_kwargs["job_id"] == "job-1"
        assert call_kwargs["timeout_minutes"] == 5

    def test_callback_error_does_not_propagate(self, tmp_path):
        from dango.platform.scheduling.resilience import ResilienceConfig, run_with_resilience

        svc = _make_service(tmp_path)
        bad_callback = MagicMock(side_effect=RuntimeError("callback boom"))
        svc._on_retry_callbacks.append(bad_callback)
        func = MagicMock(side_effect=[ValueError("fail"), "ok"])

        # Should not raise — callback error is swallowed
        result = run_with_resilience(
            func,
            scheduler_service=svc,
            job_id="job-1",
            resilience=ResilienceConfig(
                max_retries=2,
                retry_delays=(0,),
                timeout_minutes=1,
            ),
        )

        assert result == "ok"
        bad_callback.assert_called_once()


@pytest.mark.unit
class TestExecuteWithTimeout:
    """Test _execute_with_timeout() timeout enforcement."""

    def test_completes_within_timeout(self):
        from dango.platform.scheduling.resilience import _execute_with_timeout

        func = MagicMock(return_value="done")
        cancel_flag = threading.Event()

        result = _execute_with_timeout(func, (), {}, timeout_seconds=60, cancel_flag=cancel_flag)

        assert result == "done"

    def test_cancel_before_execution_raises(self):
        from dango.platform.scheduling.resilience import _execute_with_timeout

        cancel_flag = threading.Event()
        cancel_flag.set()
        func = MagicMock()

        with pytest.raises(JobCancelledError, match="cancelled before execution"):
            _execute_with_timeout(func, (), {}, timeout_seconds=60, cancel_flag=cancel_flag)

        func.assert_not_called()

    def test_timeout_raises_job_timeout_error(self):
        """Verify timeout fires _raise_in_thread with JobTimeoutError."""
        from dango.platform.scheduling.resilience import _execute_with_timeout

        cancel_flag = threading.Event()

        # Use a function that blocks long enough for the timer to fire
        block_event = threading.Event()

        def blocking_func():
            block_event.wait(timeout=5)

        with pytest.raises(JobTimeoutError):
            _execute_with_timeout(
                blocking_func,
                (),
                {},
                timeout_seconds=0,  # immediate timeout
                cancel_flag=cancel_flag,
            )


@pytest.mark.unit
class TestRaiseInThread:
    """Test _raise_in_thread() ctypes injection."""

    def test_raises_in_target_thread(self):
        from dango.platform.scheduling.resilience import _raise_in_thread

        caught = threading.Event()

        def target():
            try:
                # Loop to give the async exception a chance to fire
                while not caught.is_set():
                    pass  # Python bytecode — exception can be injected
            except JobTimeoutError:
                caught.set()

        t = threading.Thread(target=target)
        t.start()
        # Small delay to let the thread start
        import time

        time.sleep(0.01)

        _raise_in_thread(t.ident, JobTimeoutError)
        t.join(timeout=2)

        assert caught.is_set()

    def test_invalid_thread_id_logs_debug(self):
        from dango.platform.scheduling.resilience import _raise_in_thread

        with patch(f"{_RESILIENCE_MOD}.logger") as mock_logger:
            _raise_in_thread(999999999, JobTimeoutError)

        mock_logger.debug.assert_called_once()
        assert mock_logger.debug.call_args[0][0] == "raise_in_thread_invalid_id"


@pytest.mark.unit
class TestDeduplication:
    """Test max_instances=1 in job_defaults prevents overlapping runs."""

    def test_max_instances_in_job_defaults(self, tmp_path):
        with (
            patch(f"{_SCHEDULER_MOD}.SQLAlchemyJobStore"),
            patch(f"{_SCHEDULER_MOD}.AsyncIOScheduler") as mock_cls,
        ):
            mock_cls.return_value = MagicMock()
            mock_cls.return_value.running = False
            mock_cls.return_value.get_jobs.return_value = []

            from dango.platform.scheduling.scheduler import SchedulerService

            SchedulerService(tmp_path)

        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["job_defaults"]["max_instances"] == 1
        assert call_kwargs["job_defaults"]["coalesce"] is True
