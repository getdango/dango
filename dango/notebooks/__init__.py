"""dango/notebooks/__init__.py

Notebook management module — Marimo process lifecycle, DuckDB snapshots,
file locking, and proxy utilities.
"""

from dango.notebooks.locking import (
    acquire_lock,
    copy_locked_notebook,
    expire_stale_locks,
    force_release_lock,
    get_lock_info,
    is_locked,
    refresh_lock,
    release_lock,
)
from dango.notebooks.manager import (
    get_marimo_pid_file_path,
    get_marimo_status,
    start_idle_checker,
    start_marimo,
    stop_idle_checker,
    stop_marimo,
)
from dango.notebooks.snapshot import (
    cleanup_snapshots,
    create_snapshot,
    list_snapshots,
)

__all__ = [
    # manager
    "get_marimo_pid_file_path",
    "start_marimo",
    "stop_marimo",
    "get_marimo_status",
    "start_idle_checker",
    "stop_idle_checker",
    # snapshot
    "create_snapshot",
    "list_snapshots",
    "cleanup_snapshots",
    # locking
    "acquire_lock",
    "release_lock",
    "refresh_lock",
    "force_release_lock",
    "expire_stale_locks",
    "is_locked",
    "get_lock_info",
    "copy_locked_notebook",
]
