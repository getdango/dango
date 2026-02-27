"""dango/platform/scheduling/__init__.py

APScheduler-based job scheduling for Dango data pipelines.
"""

from dango.platform.scheduling.history import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    VALID_STATUSES,
    cleanup_history_job,
    cleanup_old_records,
    get_average_duration,
    get_last_run,
    get_recent_history,
    get_schedule_history,
    get_scheduler_db_path,
    record_cancellation,
    record_completion,
    record_failure,
    record_start,
    record_timeout,
)
from dango.platform.scheduling.jobs import (
    configure_jobs,
    run_scheduled_dbt,
    run_scheduled_sync,
)
from dango.platform.scheduling.resilience import (
    ResilienceConfig,
    run_with_resilience,
)
from dango.platform.scheduling.scheduler import SchedulerService

__all__ = [
    "ResilienceConfig",
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "STATUS_SUCCESS",
    "STATUS_TIMEOUT",
    "VALID_STATUSES",
    "SchedulerService",
    "cleanup_history_job",
    "cleanup_old_records",
    "configure_jobs",
    "get_average_duration",
    "get_last_run",
    "get_recent_history",
    "get_schedule_history",
    "get_scheduler_db_path",
    "record_cancellation",
    "record_completion",
    "record_failure",
    "record_start",
    "record_timeout",
    "run_scheduled_dbt",
    "run_scheduled_sync",
    "run_with_resilience",
]
