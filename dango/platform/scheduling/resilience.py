"""dango/platform/scheduling/resilience.py

Resilience features for scheduled jobs: retry with configurable backoff,
execution timeout with thread-based kill, and cancellation flags.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from dango.exceptions import JobCancelledError, JobTimeoutError
from dango.logging import get_logger

if TYPE_CHECKING:
    from dango.platform.scheduling.scheduler import SchedulerService

logger = get_logger(__name__)

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
