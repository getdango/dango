"""dango/platform/scheduling/jobs.py

Module-level job functions for APScheduler pickle serialization.

APScheduler 3.x serializes job references by module path. Functions must be
defined at module level (not as methods or lambdas) so they can be pickled
and restored from the SQLite job store across restarts.
"""


def sync_source_job(source_name: str, project_root: str) -> None:
    """Run a scheduled sync for a data source.

    Implementation in TASK-041.
    """
    raise NotImplementedError("Job implementation pending TASK-041")


def dbt_run_job(project_root: str, select: str | None = None) -> None:
    """Run a scheduled dbt transformation.

    Implementation in TASK-041.
    """
    raise NotImplementedError("Job implementation pending TASK-041")
