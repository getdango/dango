"""dango/platform/scheduling/scheduler.py

SchedulerService wraps APScheduler 3.x AsyncIOScheduler for job persistence,
event tracking, and async bridging. Includes resilience features: retry with
configurable backoff, execution timeout with thread-based kill, cancellation
flags, and event callbacks for history/notification integration.
"""

from __future__ import annotations

import asyncio
import ctypes
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import concurrent.futures

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    JobExecutionEvent,
)
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.job import Job
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from dango.exceptions import JobCancelledError, JobTimeoutError
from dango.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_THREAD_POOL_SIZE = 20
_DEFAULT_MISFIRE_GRACE_TIME = 3600  # 1 hour
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_DELAYS = (30, 120, 300)
_DEFAULT_TIMEOUT_MINUTES = 60


@dataclass(frozen=True)
class ResilienceConfig:
    """Per-schedule resilience configuration.

    Controls retry, backoff, and timeout behaviour for scheduled jobs.
    TASK-037 constructs these from ``schedules.yml`` per-schedule fields.
    """

    max_retries: int = _DEFAULT_MAX_RETRIES
    retry_delays: tuple[int, ...] = _DEFAULT_RETRY_DELAYS
    timeout_minutes: int = _DEFAULT_TIMEOUT_MINUTES

    def __post_init__(self) -> None:
        if self.max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        if not self.retry_delays:
            raise ValueError("retry_delays must not be empty")
        if self.timeout_minutes <= 0:
            raise ValueError("timeout_minutes must be positive")


class SchedulerService:
    """Wrapper around APScheduler AsyncIOScheduler.

    Provides job persistence via SQLite, event logging, and an async
    coroutine bridge for thread-pool jobs that need to broadcast via
    WebSocket.
    """

    def __init__(self, project_root: Path) -> None:
        dango_dir = project_root / ".dango"
        dango_dir.mkdir(parents=True, exist_ok=True)

        db_path = dango_dir / "scheduler.db"
        job_store_url = f"sqlite:///{db_path}"

        jobstores: dict[str, SQLAlchemyJobStore] = {
            "default": SQLAlchemyJobStore(url=job_store_url),
        }
        executors: dict[str, ThreadPoolExecutor] = {
            "default": ThreadPoolExecutor(_DEFAULT_THREAD_POOL_SIZE),
        }
        job_defaults: dict[str, Any] = {
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": _DEFAULT_MISFIRE_GRACE_TIME,
        }

        self._scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._project_root = project_root
        self._started = False

        # Cancellation flags: job_id -> threading.Event (set = cancel requested).
        # Python GIL ensures dict.__setitem__ and dict.pop are atomic.
        self._cancel_flags: dict[str, threading.Event] = {}

        # Event callbacks for resilience events.
        # TASK-039 (history) and TASK-041 (notifications) register listeners.
        self._on_retry_callbacks: list[Callable[..., None]] = []
        self._on_timeout_callbacks: list[Callable[..., None]] = []
        self._on_cancel_callbacks: list[Callable[..., None]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the scheduler and register event listeners.

        Calling ``start()`` more than once is a no-op (listeners are
        registered exactly once).

        Args:
            loop: The running asyncio event loop (for coroutine bridging).
        """
        if self._started:
            logger.warning("scheduler_already_started")
            return

        self._loop = loop

        from dango.platform.scheduling.jobs import configure_jobs

        configure_jobs(loop)

        self._scheduler.add_listener(self._on_job_executed, EVENT_JOB_EXECUTED)
        self._scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)
        self._scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)

        self._scheduler.start()
        self._started = True

        self._log_startup_summary()
        self._check_dual_scheduler()

    def shutdown(self, wait: bool = True) -> None:
        """Gracefully shut down the scheduler.

        Sets all cancel flags so threads blocked in ``cancel_flag.wait()``
        unblock immediately, then delegates to the underlying scheduler.

        Args:
            wait: If True, wait for running jobs to finish before returning.
        """
        # Signal all running jobs to cancel
        for flag in self._cancel_flags.values():
            flag.set()

        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            logger.info("scheduler_shutdown", wait=wait)

        self._cancel_flags.clear()

    # ------------------------------------------------------------------
    # Job management
    # ------------------------------------------------------------------

    def add_job(self, func: Any, trigger: Any, **kwargs: Any) -> Job:
        """Add a job to the scheduler.

        Delegates directly to ``AsyncIOScheduler.add_job()``.
        """
        return self._scheduler.add_job(func, trigger, **kwargs)

    def remove_job(self, job_id: str) -> None:
        """Remove a job by ID."""
        self._scheduler.remove_job(job_id)

    def get_jobs(self) -> list[Job]:
        """Return all scheduled jobs."""
        result: list[Job] = self._scheduler.get_jobs()
        return result

    def get_status(self) -> dict[str, Any]:
        """Return scheduler status for the health endpoint.

        Returns:
            Dict with ``running``, ``job_count``, and ``next_run_time`` keys.
        """
        jobs = self._scheduler.get_jobs() if self._scheduler.running else []
        next_run: str | None = None
        if jobs:
            upcoming = sorted(
                (j for j in jobs if j.next_run_time is not None),
                key=lambda j: j.next_run_time,
            )
            if upcoming:
                next_run = upcoming[0].next_run_time.isoformat()

        return {
            "running": self._scheduler.running,
            "job_count": len(jobs),
            "next_run_time": next_run,
        }

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel_job(self, job_id: str) -> bool:
        """Request cancellation of a running job.

        Sets the cancel flag for the given job. The job function must check
        ``is_cancelled()`` between pipeline steps to honour the request.
        The cancel API endpoint is deferred to TASK-038.

        Returns:
            True if a cancel flag was found and set, False if the job is
            not currently running (no registered cancel flag).
        """
        flag = self._cancel_flags.get(job_id)
        if flag is None:
            return False
        flag.set()
        logger.info("job_cancel_requested", job_id=job_id)
        return True

    def is_cancelled(self, job_id: str) -> bool:
        """Check whether cancellation has been requested for a job.

        Called by job functions between pipeline steps.
        """
        flag = self._cancel_flags.get(job_id)
        return flag is not None and flag.is_set()

    def _register_cancel_flag(self, job_id: str) -> threading.Event:
        """Create and store a cancel flag for a starting job."""
        if job_id in self._cancel_flags:
            logger.warning("cancel_flag_overwrite", job_id=job_id)
        flag = threading.Event()
        self._cancel_flags[job_id] = flag
        return flag

    def _clear_cancel_flag(self, job_id: str) -> None:
        """Remove the cancel flag after a job completes."""
        self._cancel_flags.pop(job_id, None)

    # ------------------------------------------------------------------
    # Async bridge
    # ------------------------------------------------------------------

    def run_coroutine(self, coro: Any) -> concurrent.futures.Future[Any]:
        """Schedule an async coroutine from a thread-pool job.

        Uses ``asyncio.run_coroutine_threadsafe`` to bridge from the
        synchronous thread-pool executor back to the event loop. Useful
        for WebSocket broadcasts triggered by sync/dbt jobs.

        Args:
            coro: An awaitable coroutine.

        Returns:
            A ``concurrent.futures.Future`` wrapping the coroutine result.

        Raises:
            RuntimeError: If the scheduler has not been started (no loop).
        """
        if self._loop is None:
            raise RuntimeError("Scheduler not started — no event loop available")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    def _on_job_executed(self, event: JobExecutionEvent) -> None:
        logger.info(
            "scheduler_job_executed",
            job_id=event.job_id,
            scheduled_run_time=str(event.scheduled_run_time),
        )

    def _on_job_error(self, event: JobExecutionEvent) -> None:
        logger.error(
            "scheduler_job_error",
            job_id=event.job_id,
            scheduled_run_time=str(event.scheduled_run_time),
            exception=str(event.exception),
            traceback=str(event.traceback),
        )

    def _on_job_missed(self, event: JobExecutionEvent) -> None:
        logger.warning(
            "scheduler_job_missed",
            job_id=event.job_id,
            scheduled_run_time=str(event.scheduled_run_time),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_startup_summary(self) -> None:
        """Log the number of loaded jobs and next run times."""
        jobs = self._scheduler.get_jobs()
        next_runs = {j.id: str(j.next_run_time) for j in jobs if j.next_run_time is not None}
        logger.info(
            "scheduler_started",
            job_count=len(jobs),
            next_run_times=next_runs,
        )

    def _check_dual_scheduler(self) -> None:
        """Warn if a cloud deployment exists while running locally.

        Prevents confusion when both local and cloud schedulers are active.
        """
        try:
            from dango.config.loader import ConfigLoader

            loader = ConfigLoader(self._project_root)
            cloud_cfg = loader.load_cloud_config()
            if cloud_cfg is not None and cloud_cfg.droplet_id is not None:
                logger.warning(
                    "dual_scheduler_warning",
                    message=(
                        "A cloud deployment is active (droplet_id="
                        f"{cloud_cfg.droplet_id}). Running a local scheduler "
                        "alongside the cloud scheduler may cause duplicate job "
                        "execution."
                    ),
                )
        except Exception:  # noqa: BLE001
            logger.debug("dual_scheduler_check_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level resilience functions
# ---------------------------------------------------------------------------


def _raise_in_thread(thread_id: int, exc_type: type[BaseException]) -> None:
    """Inject an exception into a running thread via ctypes.

    CPython-specific: uses ``PyThreadState_SetAsyncExc``. Only works when
    the target thread executes Python bytecode. If blocked in a C extension
    (e.g., DuckDB query), the exception is deferred until the C call returns.
    DbtLock timeout and DuckDB query timeouts serve as additional backstops.
    """
    res: int = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_id),
        ctypes.py_object(exc_type),
    )
    if res == 0:
        logger.debug("raise_in_thread_invalid_id", thread_id=thread_id)
    elif res > 1:
        # Multiple threads affected — should never happen. Revert.
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), None)
        logger.error("raise_in_thread_multi_affected", thread_id=thread_id)


def _execute_with_timeout(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    timeout_seconds: int,
    cancel_flag: threading.Event,
) -> Any:
    """Run *func* with a timeout enforced via a daemon Timer.

    If the cancel flag is set before starting, raises ``JobCancelledError``
    immediately. On timeout, injects ``JobTimeoutError`` into the worker
    thread.
    """
    if cancel_flag.is_set():
        raise JobCancelledError("Job cancelled before execution")

    thread_id = threading.current_thread().ident
    if thread_id is None:
        raise RuntimeError("Cannot determine current thread ID")

    timer = threading.Timer(
        timeout_seconds,
        _raise_in_thread,
        args=(thread_id, JobTimeoutError),
    )
    timer.daemon = True
    timer.start()
    try:
        return func(*args, **kwargs)
    finally:
        timer.cancel()


def run_with_resilience(
    func: Callable[..., Any],
    *args: Any,
    scheduler_service: SchedulerService,
    job_id: str,
    resilience: ResilienceConfig | None = None,
    **kwargs: Any,
) -> Any:
    """Execute a job function with retry, timeout, and cancellation.

    Called from ``jobs.py`` (TASK-041) as the top-level wrapper around
    actual sync/dbt job functions. Manages cancel flag lifecycle and
    fires event callbacks for history (TASK-039) and notifications
    (TASK-041).

    Args:
        func: The job function to execute.
        *args: Positional arguments forwarded to *func*.
        scheduler_service: The SchedulerService instance (for cancel
            flag management and callbacks).
        job_id: The APScheduler job ID.
        resilience: Per-schedule resilience config. Uses defaults if None.
        **kwargs: Keyword arguments forwarded to *func*.

    Returns:
        The return value of *func*.

    Raises:
        JobTimeoutError: If the job exceeds its timeout (no retry).
        JobCancelledError: If the job is cancelled.
        Exception: The last exception if all retries are exhausted.
    """
    cfg = resilience if resilience is not None else ResilienceConfig()
    timeout_seconds = cfg.timeout_minutes * 60
    cancel_flag = scheduler_service._register_cancel_flag(job_id)
    last_exception: BaseException | None = None

    try:
        for attempt in range(1, cfg.max_retries + 1):
            if cancel_flag.is_set():
                raise JobCancelledError(f"Job {job_id} cancelled before attempt {attempt}")

            try:
                return _execute_with_timeout(func, args, kwargs, timeout_seconds, cancel_flag)
            except JobTimeoutError:
                _fire_callbacks(
                    scheduler_service._on_timeout_callbacks,
                    job_id=job_id,
                    timeout_minutes=cfg.timeout_minutes,
                )
                raise
            except JobCancelledError:
                _fire_callbacks(
                    scheduler_service._on_cancel_callbacks,
                    job_id=job_id,
                )
                raise
            except Exception as exc:  # noqa: BLE001
                last_exception = exc
                if attempt < cfg.max_retries:
                    delay_index = min(attempt - 1, len(cfg.retry_delays) - 1)
                    delay = cfg.retry_delays[delay_index]
                    _fire_callbacks(
                        scheduler_service._on_retry_callbacks,
                        job_id=job_id,
                        attempt=attempt,
                        max_retries=cfg.max_retries,
                        next_retry_delay=delay,
                        error=str(exc),
                    )
                    logger.warning(
                        "job_retry",
                        job_id=job_id,
                        attempt=attempt,
                        next_retry_delay=delay,
                        error=str(exc),
                    )
                    # Interruptible sleep — cancel_flag.wait() returns True
                    # if the flag is set (cancellation), False on timeout
                    # (delay elapsed normally).
                    if cancel_flag.wait(timeout=delay):
                        raise JobCancelledError(
                            f"Job {job_id} cancelled during retry wait"
                        ) from exc

        # All retries exhausted — propagate the last exception
        if last_exception is not None:
            raise last_exception

    finally:
        scheduler_service._clear_cancel_flag(job_id)

    # Unreachable, but satisfies type checker
    raise RuntimeError("run_with_resilience: unreachable")  # pragma: no cover


def _fire_callbacks(
    callbacks: list[Callable[..., None]],
    **kwargs: Any,
) -> None:
    """Fire event callbacks, catching and logging any errors.

    Each callback is wrapped in try/except — callbacks are fire-and-forget.
    Matches the "never raises" pattern from webhook.py.
    """
    for cb in callbacks:
        try:
            cb(**kwargs)
        except Exception:  # noqa: BLE001
            logger.warning("callback_error", exc_info=True)
