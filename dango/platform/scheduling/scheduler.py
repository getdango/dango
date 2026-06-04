"""dango/platform/scheduling/scheduler.py

SchedulerService wraps APScheduler 3.x AsyncIOScheduler for job persistence,
event tracking, and async bridging. Resilience features (retry, timeout,
cancellation) are in scheduling/resilience.py.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import concurrent.futures

from collections.abc import Callable

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

from dango.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_THREAD_POOL_SIZE = 20
_DEFAULT_MISFIRE_GRACE_TIME = None  # Unlimited — missed jobs always run once on startup


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
        # Maps job_id → execution_history record_id for event listeners.
        # Thread-safe under CPython GIL (dict ops are atomic). Revisit if
        # targeting free-threaded Python (PEP 703).
        self._running_records: dict[str, int] = {}

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

        configure_jobs(loop, self)

        self._scheduler.add_listener(self._on_job_executed, EVENT_JOB_EXECUTED)
        self._scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)
        self._scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)

        # Log missed jobs that will be recovered on start
        self._log_missed_recovery()

        self._scheduler.start()
        self._started = True

        self._on_retry_callbacks.append(self._on_retry_event)

        # Load user-defined schedules from .dango/schedules.yml
        try:
            from dango.config.schedules import load_schedules_config, reload_schedules

            config = load_schedules_config(self._project_root)
            if config.schedules:
                result = reload_schedules(self, config.schedules, self._project_root)
                logger.info(
                    "schedules_loaded",
                    added=len(result.added),
                    skipped_disabled=len(config.schedules) - len(result.added),
                )
        except Exception:  # noqa: BLE001
            logger.error("schedules_load_failed", exc_info=True)

        self._setup_history_cleanup()
        self._setup_login_attempts_cleanup()
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

    def register_execution(self, job_id: str, record_id: int) -> None:
        """Register an execution history record for a running job.

        Called by job functions (TASK-041) after ``record_start()`` so that
        event listeners can look up the record ID when the job completes
        or fails.

        Args:
            job_id: The APScheduler job ID.
            record_id: The execution_history row ID from ``record_start()``.
        """
        self._running_records[job_id] = record_id

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
        record_id = self._running_records.pop(event.job_id, None)
        if record_id is not None:
            try:
                from dango.platform.scheduling.history import (
                    get_scheduler_db_path,
                    record_completion,
                )

                db_path = get_scheduler_db_path(self._project_root)
                record_completion(db_path, record_id)
            except Exception:  # noqa: BLE001
                logger.debug("execution_history_completion_failed", exc_info=True)

    def _on_job_error(self, event: JobExecutionEvent) -> None:
        logger.error(
            "scheduler_job_error",
            job_id=event.job_id,
            scheduled_run_time=str(event.scheduled_run_time),
            exception=str(event.exception),
            traceback=str(event.traceback),
        )
        record_id = self._running_records.pop(event.job_id, None)
        if record_id is not None:
            try:
                from dango.platform.scheduling.history import (
                    get_scheduler_db_path,
                    record_failure,
                )

                db_path = get_scheduler_db_path(self._project_root)
                record_failure(db_path, record_id, str(event.exception))
            except Exception:  # noqa: BLE001
                logger.debug("execution_history_failure_record_failed", exc_info=True)

    def _on_job_missed(self, event: JobExecutionEvent) -> None:
        logger.warning(
            "scheduler_job_missed",
            job_id=event.job_id,
            scheduled_run_time=str(event.scheduled_run_time),
        )

    # ------------------------------------------------------------------
    # Retry callback
    # ------------------------------------------------------------------

    def _on_retry_event(self, **kwargs: Any) -> None:
        """Broadcast a sync_retrying WebSocket event and send webhook.

        Registered as a retry callback by ``start()``. Never raises.
        """
        try:
            job_id: str = kwargs.get("job_id", "")
            attempt: int = kwargs.get("attempt", 0)
            max_retries: int = kwargs.get("max_retries", 0)
            error: str = kwargs.get("error", "")

            schedule_name = job_id.removeprefix("schedule:")

            from dango.platform.scheduling.jobs import _broadcast

            _broadcast(
                {
                    "event": "sync_retrying",
                    "schedule": schedule_name,
                    "attempt": attempt,
                    "max_retries": max_retries,
                    "error": error,
                    "timestamp": _ts(),
                }
            )

            from dango.platform.notifications.webhook import (
                EventType,
                WebhookSender,
                load_notification_config,
            )

            notif_config = load_notification_config(self._project_root)
            sender = WebhookSender(notif_config)
            if sender.is_configured and self._loop is not None:
                coro = sender.send(
                    event_type=EventType.SYNC_FAILED,
                    schedule_name=schedule_name,
                    error=f"Retrying (attempt {attempt}/{max_retries}): {error}",
                )
                asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception:  # noqa: BLE001
            logger.warning("retry_event_callback_error", exc_info=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _setup_history_cleanup(self) -> None:
        """Run initial cleanup and register daily cleanup job."""
        try:
            from dango.platform.scheduling.history import (
                cleanup_history_job,
                cleanup_old_records,
                get_scheduler_db_path,
            )

            db_path = get_scheduler_db_path(self._project_root)
            if db_path.exists():
                cleanup_old_records(db_path)

            self._scheduler.add_job(
                cleanup_history_job,
                "interval",
                hours=24,
                args=[str(self._project_root)],
                id="dango-internal:history-cleanup",
                replace_existing=True,
            )
        except Exception:  # noqa: BLE001
            logger.debug("history_cleanup_setup_failed", exc_info=True)

    def _setup_login_attempts_cleanup(self) -> None:
        """Register periodic login attempt cleanup (every 6 hours)."""
        try:
            from dango.auth.lockout import cleanup_login_attempts_job

            self._scheduler.add_job(
                cleanup_login_attempts_job,
                "interval",
                hours=6,
                args=[str(self._project_root)],
                id="dango-internal:login-attempts-cleanup",
                replace_existing=True,
            )
        except Exception:  # noqa: BLE001
            logger.debug("login_attempts_cleanup_setup_failed", exc_info=True)

    def _log_missed_recovery(self) -> None:
        """Log count of missed jobs that will be recovered on startup."""
        from datetime import datetime, timezone

        jobs = self._scheduler.get_jobs()
        now = datetime.now(timezone.utc)
        missed = [j for j in jobs if j.next_run_time is not None and j.next_run_time < now]
        if missed:
            logger.info(
                "scheduler_recovering_missed_jobs",
                count=len(missed),
                job_ids=[j.id for j in missed],
                message=f"Recovering {len(missed)} missed job(s) (coalesced to single execution)",
            )

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
        """Warn if running on cloud server (local scheduler may also be active)."""
        try:
            from dango.config.helpers import is_running_on_cloud

            if is_running_on_cloud():
                logger.warning(
                    "dual_scheduler_warning",
                    message=(
                        "Running scheduler on cloud server. Ensure no local "
                        "scheduler is also active to avoid duplicate job execution."
                    ),
                )
        except Exception:  # noqa: BLE001
            logger.debug("dual_scheduler_check_failed", exc_info=True)


def _ts() -> str:
    """UTC ISO timestamp for broadcast messages."""
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat()
