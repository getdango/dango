"""dango/platform/watcher.py

Backwards-compatible re-export shim. Canonical location: dango.platform.local.watcher.

Monitors data directories for changes and triggers sync operations.
"""

from dango.platform.local.watcher import (
    DebouncedFileHandler,
    FileWatcher,
    MultiTargetWatcher,
    SyncTrigger,
)

__all__ = [
    "DebouncedFileHandler",
    "FileWatcher",
    "MultiTargetWatcher",
    "SyncTrigger",
]
