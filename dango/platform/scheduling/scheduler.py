"""dango/platform/scheduling/scheduler.py

SchedulerService wraps APScheduler 3.x AsyncIOScheduler for job persistence,
event tracking, and async bridging. All subsequent Phase 4 scheduling tasks
build on this foundation.
"""

from __future__ import annotations

import asyncio
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

from dango.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_THREAD_POOL_SIZE = 20
_DEFAULT_MISFIRE_GRACE_TIME = 3600  # 1 hour


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

        self._scheduler.add_listener(self._on_job_executed, EVENT_JOB_EXECUTED)
        self._scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)
        self._scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)

        self._scheduler.start()
        self._started = True

        self._log_startup_summary()
        self._check_dual_scheduler()

    def shutdown(self, wait: bool = True) -> None:
        """Gracefully shut down the scheduler.

        Args:
            wait: If True, wait for running jobs to finish before returning.
        """
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            logger.info("scheduler_shutdown", wait=wait)

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
