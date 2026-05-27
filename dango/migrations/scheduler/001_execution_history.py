"""dango/migrations/scheduler/001_execution_history.py

Create the execution_history table for tracking scheduled job runs.
"""

from __future__ import annotations

import sqlite3

VERSION = 1
DESCRIPTION = "Create execution_history table"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create execution_history table and indexes.

    Do not call ``conn.commit()`` — the migration runner manages the
    transaction.
    """
    conn.execute(
        """
        CREATE TABLE execution_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_name    TEXT NOT NULL,
            sources          TEXT,
            started_at       TEXT NOT NULL,
            ended_at         TEXT,
            status           TEXT NOT NULL DEFAULT 'running',
            error            TEXT,
            duration_seconds REAL,
            rows_processed   INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX idx_exec_schedule_name ON execution_history (schedule_name)")
    conn.execute("CREATE INDEX idx_exec_started_at ON execution_history (started_at)")
    conn.execute("CREATE INDEX idx_exec_status ON execution_history (status)")
