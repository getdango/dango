"""dango/platform/scheduling/__init__.py

APScheduler-based job scheduling for Dango data pipelines.
"""

from dango.platform.scheduling.scheduler import SchedulerService

__all__ = ["SchedulerService"]
