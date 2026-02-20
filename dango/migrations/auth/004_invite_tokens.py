"""dango/migrations/auth/004_invite_tokens.py

Add invite token columns to users table.

Invite links (TASK-100) let admins onboard users without sharing temporary
passwords.  The token hash is stored alongside an expiry timestamp so users
can set their own password via a time-limited URL.
"""

from __future__ import annotations

import sqlite3

VERSION = 4
DESCRIPTION = "Add invite token columns"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add invite_token_hash and invite_expires_at columns to users."""
    conn.execute("ALTER TABLE users ADD COLUMN invite_token_hash TEXT")
    conn.execute("ALTER TABLE users ADD COLUMN invite_expires_at TEXT")
