"""dango/platform/local/__init__.py

Local-only platform components (nginx, file watcher).

These components are specific to the local `dango start` workflow. Cloud deployments
use different mechanisms (Caddy for routing, no file watcher).
"""

from .network import HostsManager, NetworkConfig, NginxManager
from .watcher import DebouncedFileHandler, FileWatcher, MultiTargetWatcher, SyncTrigger
from .watcher_lifecycle import (
    get_watcher_pid_file_path,
    get_watcher_status,
    start_file_watcher,
    stop_file_watcher,
)
from .watcher_runner import main as run_watcher

__all__ = [
    # network
    "NetworkConfig",
    "NginxManager",
    "HostsManager",
    # watcher
    "DebouncedFileHandler",
    "FileWatcher",
    "MultiTargetWatcher",
    "SyncTrigger",
    # watcher_lifecycle
    "get_watcher_pid_file_path",
    "get_watcher_status",
    "start_file_watcher",
    "stop_file_watcher",
    # watcher_runner
    "run_watcher",
]
