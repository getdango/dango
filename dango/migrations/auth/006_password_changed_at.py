"""dango/migrations/auth/006_password_changed_at.py

Add password_changed_at column for password rotation policy.
"""

from __future__ import annotations

import sqlite3

VERSION = 6
DESCRIPTION = "Add password_changed_at column"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add password_changed_at to users table."""
    conn.execute("ALTER TABLE users ADD COLUMN password_changed_at TEXT")
