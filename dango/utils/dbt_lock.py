"""
File-based inter-process lock for dbt operations.

Prevents concurrent dbt runs from UI, CLI, and sync operations to avoid
DuckDB locking conflicts and data corruption.
"""

import os
import sys
import json
import psutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
from contextlib import contextmanager

# Platform-specific file locking
if sys.platform == 'win32':
    import msvcrt
else:
    import fcntl


class DbtLockError(Exception):
    """Raised when unable to acquire dbt lock."""

    def __init__(self, message: str, lock_info: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.lock_info = lock_info


class DbtLock:
    """
    File-based lock for dbt operations.

    Usage:
        with DbtLock(project_root, source="cli", operation="dbt run"):
            # Perform dbt operation
            pass

    Or:
        lock = DbtLock(project_root, source="ui", operation="dbt run stg_users")
        try:
            lock.acquire()
            # Perform operation
        finally:
            lock.release()
    """

    def __init__(self, project_root: Path, source: str = "unknown", operation: str = "dbt operation"):
        """
        Initialize the lock.

        Args:
            project_root: Path to the project root directory
            source: Source of the lock (e.g., "ui", "cli", "sync")
            operation: Description of the operation being performed
        """
        self.project_root = Path(project_root)
        self.source = source
        self.operation = operation

        # Ensure state directory exists
        self.state_dir = self.project_root / ".dango" / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Lock file paths
        self.lock_file_path = self.state_dir / "dbt.lock"
        self.lock_info_path = self.state_dir / "dbt.lock.json"

        self._lock_file = None
        self._acquired = False

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with the given PID is running."""
        try:
            process = psutil.Process(pid)
            return process.is_running()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def _read_lock_info(self) -> Optional[Dict[str, Any]]:
        """Read lock information from the lock info file."""
        if not self.lock_info_path.exists():
            return None

        try:
            with open(self.lock_info_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def _write_lock_info(self):
        """Write lock information to the lock info file."""
        # Get hostname in a cross-platform way
        try:
            import socket
            hostname = socket.gethostname()
        except Exception:
            hostname = "unknown"

        lock_info = {
            "pid": os.getpid(),
            "source": self.source,
            "operation": self.operation,
            "started_at": datetime.now().isoformat(),
            "hostname": hostname
        }

        with open(self.lock_info_path, 'w') as f:
            json.dump(lock_info, f, indent=2)

    def _cleanup_stale_lock(self) -> bool:
        """
        Clean up stale lock if the holding process no longer exists.

        Returns:
            True if a stale lock was cleaned up, False otherwise
        """
        lock_info = self._read_lock_info()
        if not lock_info:
            return False

        pid = lock_info.get("pid")
        if pid and not self._is_process_running(pid):
            # Process is dead, clean up the lock
            try:
                if self.lock_file_path.exists():
                    self.lock_file_path.unlink()
                if self.lock_info_path.exists():
                    self.lock_info_path.unlink()
                return True
            except OSError:
                pass

        return False

    def acquire(self, timeout: float = 0) -> bool:
        """
        Acquire the lock.

        Args:
            timeout: Maximum time to wait for the lock (0 = don't wait)

        Returns:
            True if lock was acquired

        Raises:
            DbtLockError: If unable to acquire the lock
        """
        if self._acquired:
            return True

        # Try to clean up stale locks first
        self._cleanup_stale_lock()

        # Try to acquire the lock
        try:
            self._lock_file = open(self.lock_file_path, 'w')

            # Platform-specific locking
            if sys.platform == 'win32':
                # Windows: use msvcrt
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                # Unix: use fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            # Successfully acquired the lock
            self._write_lock_info()
            self._acquired = True
            return True

        except (IOError, OSError) as e:
            # Lock is held by another process
            if self._lock_file:
                self._lock_file.close()
                self._lock_file = None

            lock_info = self._read_lock_info()

            # Build a helpful error message
            if lock_info:
                source = lock_info.get("source", "unknown")
                operation = lock_info.get("operation", "unknown operation")
                started_at = lock_info.get("started_at", "unknown time")
                pid = lock_info.get("pid", "unknown")

                message = (
                    f"Another dbt operation is currently running.\n"
                    f"Source: {source}\n"
                    f"Operation: {operation}\n"
                    f"Started at: {started_at}\n"
                    f"Process ID: {pid}\n"
                    f"Please wait for it to complete before starting a new operation."
                )
            else:
                message = (
                    "Another dbt operation is currently running. "
                    "Please wait for it to complete before starting a new operation."
                )

            raise DbtLockError(message, lock_info=lock_info)

    def release(self):
        """Release the lock."""
        if not self._acquired:
            return

        try:
            if self._lock_file:
                # Platform-specific unlocking
                if sys.platform == 'win32':
                    # Windows: use msvcrt
                    try:
                        msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    except (IOError, OSError):
                        pass  # Lock may already be released
                else:
                    # Unix: use fcntl
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)

                self._lock_file.close()
                self._lock_file = None

            # Clean up lock files
            if self.lock_file_path.exists():
                self.lock_file_path.unlink()
            if self.lock_info_path.exists():
                self.lock_info_path.unlink()

            self._acquired = False
        except (IOError, OSError):
            pass

    def __enter__(self):
        """Context manager entry."""
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.release()
        return False

    def __del__(self):
        """Cleanup on deletion."""
        self.release()


@contextmanager
def dbt_lock(project_root: Path, source: str = "unknown", operation: str = "dbt operation"):
    """
    Context manager for dbt lock.

    Usage:
        with dbt_lock(project_root, source="cli", operation="dbt run"):
            # Perform dbt operation
            pass
    """
    lock = DbtLock(project_root, source=source, operation=operation)
    try:
        lock.acquire()
        yield lock
    finally:
        lock.release()
