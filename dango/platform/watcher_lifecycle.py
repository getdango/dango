"""dango/platform/watcher_lifecycle.py

Backwards-compatible re-export shim. Canonical location: dango.platform.local.watcher_lifecycle.

Watcher subprocess lifecycle management (start, stop, status).
"""

from dango.platform.local.watcher_lifecycle import (
    get_watcher_pid_file_path,
    get_watcher_status,
    kill_orphan_watchers,
    start_file_watcher,
    stop_file_watcher,
)

__all__ = [
    "get_watcher_pid_file_path",
    "get_watcher_status",
    "kill_orphan_watchers",
    "start_file_watcher",
    "stop_file_watcher",
]
