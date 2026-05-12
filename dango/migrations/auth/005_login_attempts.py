"""dango/migrations/auth/005_login_attempts.py

Add login_attempts table for IP-based brute-force tracking.

BUG-235: The original lockout tracked attempts only in the ``users``
table, so non-existent emails never triggered lockout responses.  An
attacker could enumerate valid emails by observing whether repeated
login failures eventually returned HTTP 423 (existing) vs always
returning HTTP 400 (non-existent).

This migration adds a separate ``login_attempts`` table keyed by the
SHA-256 hash of ``ip:email``, giving identical lockout behaviour
regardless of whether the email belongs to a real user.
"""

from __future__ import annotations

import sqlite3

VERSION = 5
DESCRIPTION = "Add login_attempts table for IP-based lockout"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the login_attempts table with indexes."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            key TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_email ON login_attempts (email)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_updated ON login_attempts (updated_at)"
    )
