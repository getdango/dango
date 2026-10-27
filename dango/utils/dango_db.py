"""dango/utils/dango_db.py

Connection management for the Dango metadata database (``.dango/dango.db``).

Provides a single entry point — :func:`get_connection` — that lazily creates
the ``.dango/`` directory, opens a WAL-mode SQLite connection, and ensures
all legacy tables exist, then applies the serialized metadata migration stream.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from dango.logging import get_logger

logger = get_logger(__name__)

# Tracks db paths whose schema has already been ensured this process.
_schema_initialized: set[str] = set()


def _enable_wal(conn: sqlite3.Connection, *, timeout_seconds: float = 5.0) -> None:
    """Retry only transient first-open WAL lock contention, with no native wait.

    SQLite can reject simultaneous journal-mode changes without invoking its
    busy handler. The caller opens with timeout=0 so each attempt is nonblocking;
    a monotonic deadline bounds retries. Other operational failures propagate.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            # Exact SQLite SQLITE_BUSY message; also works on Python 3.10,
            # where sqlite_errorcode is not available on the exception.
            if str(exc) != "database is locked":
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.05, remaining))


@contextmanager
def _metadata_setup_lock(
    db_path: Path, *, timeout_seconds: float = 5.0
) -> Generator[None, None, None]:
    """Serialize connection preparation with the persistent metadata lock."""
    import fcntl

    from dango.exceptions import MigrationApplicationError

    deadline = time.monotonic() + timeout_seconds
    with (db_path.parent / "metadata-migration.lock").open("a+b") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MigrationApplicationError(
                        "Timed out waiting for metadata connection setup at "
                        ".dango/metadata-migration.lock. Wait for the other process to finish "
                        "and retry. Do not delete the lock file."
                    ) from None
                time.sleep(min(0.05, remaining))
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def get_dango_db_path(project_root: Path) -> Path:
    """Return the path to ``.dango/dango.db``.

    Args:
        project_root: Path to the Dango project root.

    Returns:
        Absolute path to the dango metadata database file.
    """
    return project_root / ".dango" / "dango.db"


def get_connection(project_root: Path) -> sqlite3.Connection:
    """Open a connection to ``.dango/dango.db``, creating it if needed.

    Creates the ``.dango/`` directory if it does not exist, opens the database
    in WAL mode with foreign keys enabled, and ensures all required tables
    are present.

    Connection preparation waits up to five seconds for the persistent metadata
    lock, before opening SQLite. WAL setup also bounds transient lock retries;
    normal SQLite busy waiting is restored before legacy setup.

    Callers are responsible for closing the returned connection.  Prefer
    :func:`connect` (context manager) for automatic cleanup.

    Args:
        project_root: Path to the Dango project root.

    Returns:
        A ``sqlite3.Connection`` with ``row_factory = sqlite3.Row``.
    """
    db_path = get_dango_db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = None
    db_key = str(db_path)
    try:
        # Open only after acquiring the same persistent inode used by bootstrap.
        # Competing journal transitions cannot be made safe by SQLite busy waiting.
        with _metadata_setup_lock(db_path):
            conn = sqlite3.connect(str(db_path), timeout=0)
            conn.row_factory = sqlite3.Row
            _enable_wal(conn)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")

            if db_key not in _schema_initialized:
                _ensure_schema(conn)
            # The runner uses separate connections. Commit before releasing setup
            # ownership, then let bootstrap acquire the lock without nesting it.
            conn.commit()
        from dango.migrations.metadata.bootstrap import ensure_metadata_schema

        ensure_metadata_schema(project_root)
        _schema_initialized.add(db_key)
    except BaseException:
        if conn is not None:
            conn.close()
        _schema_initialized.discard(db_key)
        raise

    return conn


@contextmanager
def connect(project_root: Path) -> Generator[sqlite3.Connection, None, None]:
    """Context manager wrapper around :func:`get_connection`.

    Usage::

        with connect(project_root) as conn:
            conn.execute("SELECT ...")

    Args:
        project_root: Path to the Dango project root.

    Yields:
        A ``sqlite3.Connection`` that is closed on exit.
    """
    conn = get_connection(project_root)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema definition
# ---------------------------------------------------------------------------

_DDL = """\
CREATE TABLE IF NOT EXISTS profiling_stats (
    source       TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    column_name  TEXT NOT NULL,
    stat_type    TEXT NOT NULL,
    stat_value   TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (source, table_name, column_name, stat_type)
);

CREATE TABLE IF NOT EXISTS schema_baselines (
    source       TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    column_name  TEXT NOT NULL,
    column_type  TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (source, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS drift_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    column_name  TEXT,
    event_type   TEXT NOT NULL,
    detail       TEXT,
    detected_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pii_findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    column_name  TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    confidence   REAL,
    sample_count INTEGER,
    scanned_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notebook_locks (
    notebook_id  TEXT PRIMARY KEY,
    locked_by    TEXT NOT NULL,
    locked_at    TEXT NOT NULL,
    expires_at   TEXT
);

CREATE TABLE IF NOT EXISTS notebook_metadata (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name  TEXT NOT NULL,
    source       TEXT,
    table_name   TEXT,
    metric_value REAL,
    recorded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name  TEXT NOT NULL,
    source       TEXT,
    table_name   TEXT,
    result_type  TEXT NOT NULL,
    result_value TEXT,
    computed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pii_overrides (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    column_name  TEXT NOT NULL,
    pii_status   TEXT NOT NULL CHECK (pii_status IN ('pii', 'not_pii')),
    set_by       TEXT NOT NULL,
    reason       TEXT,
    updated_at   TEXT NOT NULL,
    UNIQUE (source, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS source_attention (
    source       TEXT PRIMARY KEY,
    reason       TEXT NOT NULL,
    drift_events TEXT,
    created_at   TEXT NOT NULL
);
"""

# Additive migrations — each wrapped in try/except to ignore "duplicate column".
_ADDITIVE_DDL = [
    "ALTER TABLE drift_events ADD COLUMN severity TEXT",
    "ALTER TABLE notebook_locks ADD COLUMN last_heartbeat_at TEXT",
]


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create all governance/notebook/analysis tables if they do not exist.

    Args:
        conn: An open SQLite connection.
    """
    conn.executescript(_DDL)
    for stmt in _ADDITIVE_DDL:
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:  # noqa: BLE001
            pass  # Column already exists
