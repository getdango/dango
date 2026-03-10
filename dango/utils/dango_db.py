"""dango/utils/dango_db.py

Connection management for the Dango metadata database (``.dango/dango.db``).

Provides a single entry point — :func:`get_connection` — that lazily creates
the ``.dango/`` directory, opens a WAL-mode SQLite connection, and ensures
all governance/notebook/analysis tables exist via ``CREATE TABLE IF NOT EXISTS``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from dango.logging import get_logger

logger = get_logger(__name__)


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

    Args:
        project_root: Path to the Dango project root.

    Returns:
        A ``sqlite3.Connection`` with ``row_factory = sqlite3.Row``.
    """
    db_path = get_dango_db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    _ensure_schema(conn)
    return conn


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
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create all governance/notebook/analysis tables if they do not exist.

    Args:
        conn: An open SQLite connection.
    """
    conn.executescript(_DDL)
