"""dango/utils/dbt_log_lock.py

Find DuckDB's lock conflict in the part of dbt's own log written by one dbt run.

Level 0 module. dbt's console output is not captured (live progress stays as it is); its log file
`dbt/logs/dbt.log` records the same error. Only bytes written after the recorded offset count, so
an earlier run's failure is never reported for this one. Never raises.
"""

from __future__ import annotations

from pathlib import Path

from dango.utils.duckdb_lock_text import lock_message_from_text

# The lock error is raised when dbt opens the warehouse, near the start of a run.
_MAX_READ_BYTES = 4 * 1024 * 1024


def _log_path(dbt_dir: Path) -> Path:
    return Path(dbt_dir) / "logs" / "dbt.log"


def log_size(dbt_dir: Path) -> int:
    """Current size of dbt's log in bytes (0 when it does not exist)."""
    try:
        return _log_path(dbt_dir).stat().st_size
    except OSError:
        return 0


def lock_message_since(dbt_dir: Path, offset: int) -> str | None:
    """The plain lock message when the log written after *offset* shows a lock conflict.

    A log smaller than *offset* was rotated during the run, so it is read from its start.
    """
    try:
        with open(_log_path(dbt_dir), "rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(offset if offset <= size else 0)
            raw = handle.read(_MAX_READ_BYTES)
    except OSError:
        return None
    return lock_message_from_text(raw.decode("utf-8", errors="replace"))
