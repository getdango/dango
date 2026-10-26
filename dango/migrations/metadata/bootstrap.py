"""dango/migrations/metadata/bootstrap.py

Serialize the metadata migration stream in the existing project database.
"""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path

from dango.exceptions import MigrationApplicationError
from dango.migrations.runner import MigrationRunner


def _validate_history(runner: MigrationRunner) -> None:
    """Reject history that cannot be explained by this metadata stream."""
    discovered = runner.discover_migrations()
    try:
        status = runner.status()
        expected = [(item.version, item.description) for item in discovered]
        actual = [(item.version, item.description) for item in status.applied]
        if actual != expected[: len(actual)]:
            raise MigrationApplicationError(
                "Unexplained metadata migration history in dango.db. "
                "Back up the database and inspect _migrations before retrying; "
                "do not delete or overwrite migration history."
            )
        if any(item.version == 1 for item in status.applied):
            conn = sqlite3.connect(str(runner.db_path))
            try:
                # Version 1 must describe a real journal, not just a matching integer.
                conn.execute(
                    "SELECT operation_id, project_id, actor_id, operation_name, idempotency_key, "
                    "request_digest, permission_revision, expected_revision, resource_revision, "
                    "result_ref, state, status, outcome_json, created_at, updated_at "
                    "FROM platform_operations LIMIT 0"
                )
                conn.execute(
                    "SELECT operation_id, effect_id, effect_sequence, effect_name, state, "
                    "native_ref, recovery_ref, created_at FROM platform_effects LIMIT 0"
                )
                conn.execute(
                    "SELECT project_id, holder_operation_id, lease_token, expires_at_epoch_us, "
                    "revision, updated_at FROM platform_leases LIMIT 0"
                )
            finally:
                conn.close()
    except sqlite3.DatabaseError as exc:
        raise MigrationApplicationError(
            "Metadata migration history or journal schema in dango.db is incompatible. "
            "Back up the database and inspect _migrations and the journal tables before retrying."
        ) from exc


def ensure_metadata_schema(project_root: Path, *, timeout_seconds: float = 10.0) -> None:
    """Apply pending metadata migrations under a persistent interprocess lock.

    Call after preparing the legacy schema, without holding a SQLite write
    transaction: the runner opens separate connections. Linux and macOS use
    kernel flock locks, released even on process death. Never unlink the lock
    file, since waiters must continue to lock the same inode.

    Args:
        project_root: Path to the Dango project root.
        timeout_seconds: Maximum lock wait in seconds; zero attempts once.

    Raises:
        ValueError: If the timeout is negative or not finite.
        MigrationApplicationError: On timeout, incompatible history or migration failure.
    """
    import fcntl

    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise ValueError("timeout_seconds must be finite and nonnegative")

    db_path = project_root / ".dango" / "dango.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = db_path.parent / "metadata-migration.lock"
    deadline = time.monotonic() + timeout_seconds
    with lock_path.open("a+b") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MigrationApplicationError(
                        "Timed out waiting for .dango/metadata-migration.lock. "
                        "Another process may be migrating this project; wait for it to finish "
                        "and retry, or increase timeout_seconds. Do not delete the lock file."
                    ) from None
                time.sleep(min(0.05, remaining))
        try:
            runner = MigrationRunner(db_path, "metadata", Path(__file__).parent)
            _validate_history(runner)
            runner.apply_pending()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
