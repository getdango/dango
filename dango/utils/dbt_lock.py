"""dango/utils/dbt_lock.py

Prevents concurrent dbt runs from UI, CLI, and sync operations to avoid DuckDB locking conflicts and data corruption.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from dango.exceptions import DbtLockError

logger = logging.getLogger(__name__)

# Platform-specific file locking
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


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

    def __init__(
        self, project_root: Path, source: str = "unknown", operation: str = "dbt operation"
    ):
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

        self._lock_file: IO[str] | None = None
        self._acquired = False

    def _read_lock_info(self) -> dict[str, Any] | None:
        """Read lock information from the lock info file."""
        if not self.lock_info_path.exists():
            return None

        try:
            with open(self.lock_info_path) as f:
                result: dict[str, Any] = json.load(f)
                return result
        except (OSError, json.JSONDecodeError):
            return None

    def _write_lock_info(self) -> None:
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
            "started_at": datetime.now(tz=timezone.utc).isoformat(),
            "hostname": hostname,
        }

        with open(self.lock_info_path, "w") as f:
            json.dump(lock_info, f, indent=2)

    def _cleanup_stale_lock(self) -> bool:
        """
        No-op: stale locks clean themselves up via kernel flock auto-release.

        Kept for API compatibility with acquire() call site.
        Returns False always (no cleanup performed).

        See also: startup.cleanup_stale_dbt_lock() for the proactive startup variant.
        """
        return False

    def acquire(self, timeout: float = 300) -> bool:
        """
        Acquire the lock.

        Args:
            timeout: Maximum time to wait for the lock (0 = don't wait).
                Retries every 3 seconds until timeout is reached.

        Returns:
            True if lock was acquired

        Raises:
            DbtLockError: If unable to acquire the lock
        """
        if self._acquired:
            return True

        import time

        deadline = time.monotonic() + timeout

        while True:
            # Try to clean up stale locks first
            self._cleanup_stale_lock()

            # Try to acquire the lock
            try:
                self._lock_file = open(self.lock_file_path, "w")

                # Platform-specific locking
                if sys.platform == "win32":
                    # Windows: use msvcrt
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    # Unix: use fcntl
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                # Successfully acquired the lock
                self._write_lock_info()
                self._acquired = True
                return True

            except OSError:
                # Lock is held by another process
                if self._lock_file:
                    self._lock_file.close()
                    self._lock_file = None

                # Retry if we still have time
                if time.monotonic() < deadline:
                    lock_info = self._read_lock_info()
                    if lock_info:
                        logger.info(
                            "Waiting for dbt lock... (held by PID %d)",
                            lock_info.get("pid", "unknown"),
                        )
                    else:
                        logger.info("Waiting for dbt lock...")
                    time.sleep(3)
                    continue

                lock_info = self._read_lock_info()
                message = "Sync queue timeout. Another sync is still running."

                raise DbtLockError(message, lock_info=lock_info) from None

    def release(self) -> None:
        """Release the lock."""
        if not self._acquired:
            return

        try:
            if self._lock_file:
                # Platform-specific unlocking
                if sys.platform == "win32":
                    # Windows: use msvcrt
                    try:
                        msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:  # noqa: BLE001
                        pass  # Lock may already be released
                else:
                    # Unix: use fcntl
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)

                self._lock_file.close()
                self._lock_file = None

            self._acquired = False
        except OSError:  # noqa: BLE001
            pass

    def __enter__(self) -> DbtLock:
        """Context manager entry."""
        self.acquire()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: Any
    ) -> None:
        """Context manager exit."""
        self.release()

    def __del__(self) -> None:
        """Cleanup on deletion."""
        self.release()


@contextmanager
def dbt_lock(
    project_root: Path,
    source: str = "unknown",
    operation: str = "dbt operation",
    timeout: float = 300,
) -> Generator[DbtLock, None, None]:
    """
    Context manager for dbt lock.

    Usage:
        with dbt_lock(project_root, source="cli", operation="dbt run"):
            # Perform dbt operation
            pass
    """
    lock = DbtLock(project_root, source=source, operation=operation)
    try:
        lock.acquire(timeout=timeout)
        yield lock
    finally:
        lock.release()
