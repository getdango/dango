"""dango/migrations/auth/001_initial_auth.py

Create initial authentication tables: users, sessions, and api_keys.
"""

from __future__ import annotations

import sqlite3

VERSION = 1
DESCRIPTION = "Create initial auth tables"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create users, sessions, and api_keys tables with indexes."""
    conn.execute(
        """
        CREATE TABLE users (
            id                    TEXT PRIMARY KEY,
            email                 TEXT NOT NULL UNIQUE,
            password_hash         TEXT,
            role                  TEXT NOT NULL DEFAULT 'viewer',
            is_active             INTEGER NOT NULL DEFAULT 1,
            totp_secret           TEXT,
            totp_enabled          INTEGER NOT NULL DEFAULT 0,
            recovery_codes        TEXT,
            oauth_provider        TEXT,
            oauth_id              TEXT,
            failed_login_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until          TEXT,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE sessions (
            id             TEXT PRIMARY KEY,
            user_id        TEXT NOT NULL,
            token_hash     TEXT NOT NULL UNIQUE,
            is_active      INTEGER NOT NULL DEFAULT 1,
            is_partial     INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL,
            expires_at     TEXT NOT NULL,
            last_activity  TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX idx_sessions_user_id ON sessions (user_id)")
    conn.execute("CREATE INDEX idx_sessions_expires_at ON sessions (expires_at)")

    conn.execute(
        """
        CREATE TABLE api_keys (
            id           TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL,
            name         TEXT NOT NULL,
            key_hash     TEXT NOT NULL UNIQUE,
            is_active    INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT NOT NULL,
            last_used_at TEXT,
            expires_at   TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX idx_api_keys_user_id ON api_keys (user_id)")
