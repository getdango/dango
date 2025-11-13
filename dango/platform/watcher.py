"""
File Watcher for Auto-Triggering Data Pipelines

Monitors data directories for changes and triggers sync operations
with debounce logic to batch multiple file uploads.

Created: MVP Week 1 Day 4 (Oct 27, 2025)
"""

import time
import threading
from pathlib import Path
from typing import Optional, Callable, Dict, Set, Any
from datetime import datetime, timedelta
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent


class DebouncedFileHandler(FileSystemEventHandler):
    """
    File system event handler with debounce logic.

    Waits for a quiet period before triggering the callback.
    Prevents triggering on every file change in a batch upload.
    """

    def __init__(
        self,
        callback: Callable,
        debounce_seconds: int = 600,  # 10 minutes
        watch_patterns: Optional[Set[str]] = None
    ):
        """
        Initialize handler

        Args:
            callback: Function to call after debounce period
            debounce_seconds: Seconds to wait after last change (default: 600 = 10 min)
            watch_patterns: File patterns to watch (e.g., {"*.csv"})
        """
        super().__init__()
        self.callback = callback
        self.debounce_seconds = debounce_seconds
        self.watch_patterns = watch_patterns or {"*.csv"}

        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._last_trigger_time: Optional[datetime] = None
        self._pending_files: Set[str] = set()

    def _should_process_file(self, file_path: str) -> bool:
        """Check if file matches watch patterns"""
        path = Path(file_path)

        # Ignore hidden files and directories
        if any(part.startswith('.') for part in path.parts):
            return False

        # Check if file matches any pattern
        for pattern in self.watch_patterns:
            if path.match(pattern):
                return True

        return False

    def _on_file_change(self, event: FileSystemEvent):
        """Handle file change with debounce"""
        if event.is_directory:
            return

        if not self._should_process_file(event.src_path):
            return

        with self._lock:
            # Add file to pending set
            self._pending_files.add(event.src_path)

            # Cancel existing timer
            if self._timer is not None:
                self._timer.cancel()

            # Start new timer
            self._timer = threading.Timer(
                self.debounce_seconds,
                self._trigger_callback
            )
            self._timer.daemon = True
            self._timer.start()

            print(f"[FileWatcher] Detected change: {Path(event.src_path).name}")
            print(f"[FileWatcher] Will trigger in {self.debounce_seconds}s if no more changes...")

    def _trigger_callback(self):
        """Trigger the callback after debounce period"""
        with self._lock:
            if self._pending_files:
                files = list(self._pending_files)
                self._pending_files.clear()
                self._last_trigger_time = datetime.now()

                print(f"[FileWatcher] Triggering callback for {len(files)} files")

                # Call the callback (release lock first to avoid deadlock)
                self._lock.release()
                try:
                    self.callback(files)
                finally:
                    self._lock.acquire()

    def cancel_timer(self):
        """Cancel pending timer (for manual triggers)"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
                print("[FileWatcher] Timer cancelled (manual trigger)")

    def on_created(self, event):
        """File created"""
        self._on_file_change(event)

    def on_modified(self, event):
        """File modified"""
        self._on_file_change(event)

    def on_moved(self, event):
        """File moved/renamed"""
        self._on_file_change(event)


class FileWatcher:
    """
    Monitors directories for file changes and triggers callbacks.

    Features:
    - Watches multiple directories
    - Debounces rapid changes (default: 10 minutes)
    - Filters by file patterns
    - Thread-safe
    - Can be cancelled for manual triggers
    """

    def __init__(
        self,
        callback: Callable,
        debounce_seconds: int = 600,
        watch_patterns: Optional[Set[str]] = None
    ):
        """
        Initialize file watcher

        Args:
            callback: Function to call when files change
            debounce_seconds: Seconds to wait after last change
            watch_patterns: File patterns to watch (e.g., {"*.csv", "*.json"})
        """
        self.callback = callback
        self.debounce_seconds = debounce_seconds
        self.watch_patterns = watch_patterns or {"*.csv"}

        self.observer = Observer()
        self.handler = DebouncedFileHandler(
            callback=self._wrapped_callback,
            debounce_seconds=debounce_seconds,
            watch_patterns=watch_patterns
        )

        self.watched_paths: Dict[str, Any] = {}
        self._is_running = False

    def _wrapped_callback(self, files: list):
        """Wrapper to provide additional context to callback"""
        self.callback({
            "files": files,
            "timestamp": datetime.now().isoformat(),
            "trigger": "auto"
        })

    def watch_directory(self, path: Path, recursive: bool = True):
        """
        Add directory to watch list

        Args:
            path: Directory to watch
            recursive: Watch subdirectories too
        """
        if not path.exists():
            raise ValueError(f"Directory does not exist: {path}")

        if not path.is_dir():
            raise ValueError(f"Not a directory: {path}")

        path_str = str(path.absolute())

        if path_str in self.watched_paths:
            print(f"[FileWatcher] Already watching: {path}")
            return

        # Schedule the path with observer
        watch_handle = self.observer.schedule(
            self.handler,
            str(path),
            recursive=recursive
        )

        self.watched_paths[path_str] = {
            "path": path,
            "handle": watch_handle,
            "recursive": recursive
        }

        print(f"[FileWatcher] Now watching: {path} (recursive={recursive})")

    def unwatch_directory(self, path: Path):
        """Remove directory from watch list"""
        path_str = str(path.absolute())

        if path_str not in self.watched_paths:
            return

        # Unschedule from observer
        watch_info = self.watched_paths[path_str]
        self.observer.unschedule(watch_info["handle"])

        del self.watched_paths[path_str]
        print(f"[FileWatcher] Stopped watching: {path}")

    def start(self):
        """Start watching for file changes"""
        if self._is_running:
            print("[FileWatcher] Already running")
            return

        if not self.watched_paths:
            raise ValueError("No directories to watch. Call watch_directory() first.")

        self.observer.start()
        self._is_running = True

        print(f"[FileWatcher] Started (debounce: {self.debounce_seconds}s)")
        print(f"[FileWatcher] Watching {len(self.watched_paths)} directories")

    def stop(self):
        """Stop watching for file changes"""
        if not self._is_running:
            return

        self.observer.stop()
        self.observer.join(timeout=5)
        self._is_running = False

        print("[FileWatcher] Stopped")

    def cancel_pending(self):
        """Cancel any pending triggers (for manual override)"""
        self.handler.cancel_timer()

    def is_running(self) -> bool:
        """Check if watcher is running"""
        return self._is_running

    def get_watched_directories(self) -> list:
        """Get list of watched directories"""
        return [info["path"] for info in self.watched_paths.values()]


class MultiTargetWatcher:
    """
    Watches multiple directories with different file patterns and callbacks.

    Features:
    - Support multiple watch targets (CSV, SQL, YAML, etc.)
    - Each target has its own callback and debounce settings
    - Thread-safe
    """

    def __init__(self):
        """Initialize multi-target watcher"""
        self.watchers: Dict[str, FileWatcher] = {}
        self._is_running = False

    def add_watch_target(
        self,
        name: str,
        callback: Callable,
        watch_patterns: Set[str],
        debounce_seconds: int = 600
    ):
        """
        Add a watch target

        Args:
            name: Unique name for this watch target
            callback: Function to call when files change
            watch_patterns: File patterns to watch
            debounce_seconds: Debounce period
        """
        if name in self.watchers:
            raise ValueError(f"Watch target '{name}' already exists")

        watcher = FileWatcher(
            callback=callback,
            debounce_seconds=debounce_seconds,
            watch_patterns=watch_patterns
        )

        self.watchers[name] = watcher
        print(f"[MultiTargetWatcher] Added watch target: {name} (patterns: {watch_patterns})")

    def watch_directory(self, target_name: str, path: Path, recursive: bool = True):
        """
        Add directory to a watch target

        Args:
            target_name: Name of the watch target
            path: Directory to watch
            recursive: Watch subdirectories too
        """
        if target_name not in self.watchers:
            raise ValueError(f"Watch target '{target_name}' not found")

        self.watchers[target_name].watch_directory(path, recursive)

    def start(self):
        """Start all watchers"""
        if self._is_running:
            print("[MultiTargetWatcher] Already running")
            return

        for name, watcher in self.watchers.items():
            try:
                watcher.start()
                print(f"[MultiTargetWatcher] Started: {name}")
            except Exception as e:
                print(f"[MultiTargetWatcher] Failed to start {name}: {e}")

        self._is_running = True

    def stop(self):
        """Stop all watchers"""
        if not self._is_running:
            return

        for name, watcher in self.watchers.items():
            watcher.stop()
            print(f"[MultiTargetWatcher] Stopped: {name}")

        self._is_running = False

    def cancel_pending(self, target_name: Optional[str] = None):
        """
        Cancel pending triggers

        Args:
            target_name: Specific target to cancel, or None for all
        """
        if target_name:
            if target_name in self.watchers:
                self.watchers[target_name].cancel_pending()
        else:
            for watcher in self.watchers.values():
                watcher.cancel_pending()

    def is_running(self) -> bool:
        """Check if watcher is running"""
        return self._is_running

    def get_status(self) -> Dict[str, Any]:
        """Get status of all watch targets"""
        return {
            "running": self._is_running,
            "targets": {
                name: {
                    "running": watcher.is_running(),
                    "watched_directories": [str(p) for p in watcher.get_watched_directories()]
                }
                for name, watcher in self.watchers.items()
            }
        }


class SyncTrigger:
    """
    Manages auto-triggering of sync operations with cascading to dbt.

    Workflow:
    1. File changes detected → wait 10 min → trigger sync
    2. Sync completes → wait 10 min → trigger dbt
    3. Manual trigger cancels auto-trigger
    """

    def __init__(
        self,
        sync_callback: Callable,
        dbt_callback: Optional[Callable] = None,
        sync_debounce: int = 600,
        dbt_debounce: int = 600,
        enable_auto_sync: bool = True,
        enable_auto_dbt: bool = True
    ):
        """
        Initialize sync trigger

        Args:
            sync_callback: Function to call to trigger sync
            dbt_callback: Function to call to trigger dbt
            sync_debounce: Seconds to wait before auto-sync
            dbt_debounce: Seconds to wait before auto-dbt
            enable_auto_sync: Enable automatic sync on file changes
            enable_auto_dbt: Enable automatic dbt after sync
        """
        self.sync_callback = sync_callback
        self.dbt_callback = dbt_callback
        self.sync_debounce = sync_debounce
        self.dbt_debounce = dbt_debounce
        self.enable_auto_sync = enable_auto_sync
        self.enable_auto_dbt = enable_auto_dbt

        self._dbt_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

        # Create file watcher
        self.file_watcher = FileWatcher(
            callback=self._on_files_changed,
            debounce_seconds=sync_debounce,
            watch_patterns={"*.csv"}
        )

    def _on_files_changed(self, event_data: dict):
        """Handle file change event"""
        if not self.enable_auto_sync:
            print("[SyncTrigger] Auto-sync disabled, skipping")
            return

        files = event_data["files"]
        print(f"[SyncTrigger] Auto-triggering sync for {len(files)} files")

        # Trigger sync
        try:
            self.sync_callback()

            # Schedule dbt if enabled
            if self.enable_auto_dbt and self.dbt_callback:
                self._schedule_dbt()

        except Exception as e:
            print(f"[SyncTrigger] Sync failed: {e}")

    def _schedule_dbt(self):
        """Schedule dbt trigger after debounce period"""
        with self._lock:
            # Cancel existing timer
            if self._dbt_timer is not None:
                self._dbt_timer.cancel()

            # Start new timer
            self._dbt_timer = threading.Timer(
                self.dbt_debounce,
                self._trigger_dbt
            )
            self._dbt_timer.daemon = True
            self._dbt_timer.start()

            print(f"[SyncTrigger] Will trigger dbt in {self.dbt_debounce}s...")

    def _trigger_dbt(self):
        """Trigger dbt run"""
        print("[SyncTrigger] Auto-triggering dbt")

        try:
            self.dbt_callback()
        except Exception as e:
            print(f"[SyncTrigger] dbt failed: {e}")

    def manual_sync(self):
        """Manually trigger sync (cancels auto-trigger)"""
        # Cancel pending auto-sync
        self.file_watcher.cancel_pending()

        print("[SyncTrigger] Manual sync triggered")
        self.sync_callback()

        # Still schedule dbt if enabled
        if self.enable_auto_dbt and self.dbt_callback:
            self._schedule_dbt()

    def manual_dbt(self):
        """Manually trigger dbt (cancels auto-trigger)"""
        with self._lock:
            if self._dbt_timer is not None:
                self._dbt_timer.cancel()
                self._dbt_timer = None

        print("[SyncTrigger] Manual dbt triggered")
        if self.dbt_callback:
            self.dbt_callback()

    def watch_directory(self, path: Path):
        """Add directory to watch list"""
        self.file_watcher.watch_directory(path)

    def start(self):
        """Start watching for file changes"""
        if self.enable_auto_sync:
            self.file_watcher.start()
            print("[SyncTrigger] Auto-sync enabled")
        else:
            print("[SyncTrigger] Auto-sync disabled")

    def stop(self):
        """Stop watching"""
        self.file_watcher.stop()

        # Cancel pending dbt
        with self._lock:
            if self._dbt_timer is not None:
                self._dbt_timer.cancel()
                self._dbt_timer = None

    def get_status(self) -> dict:
        """Get current trigger status"""
        return {
            "file_watcher_running": self.file_watcher.is_running(),
            "auto_sync_enabled": self.enable_auto_sync,
            "auto_dbt_enabled": self.enable_auto_dbt,
            "sync_debounce": self.sync_debounce,
            "dbt_debounce": self.dbt_debounce,
            "watched_directories": [
                str(p) for p in self.file_watcher.get_watched_directories()
            ]
        }
