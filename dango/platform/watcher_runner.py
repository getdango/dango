"""dango/platform/watcher_runner.py

Backwards-compatible re-export shim. Canonical location: dango.platform.local.watcher_runner.

Background process entry point for file watcher.
"""

from dango.platform.local.watcher_runner import main

__all__ = [
    "main",
]
