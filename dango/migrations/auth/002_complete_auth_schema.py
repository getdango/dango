"""dango/migrations/auth/002_complete_auth_schema.py

Add deferred fields: metabase_user_id, must_change_password, last_login,
ip_address, user_agent, key_prefix.
"""

from __future__ import annotations

import sqlite3

VERSION = 2
DESCRIPTION = "Add deferred auth schema fields"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add deferred fields to users, sessions, and api_keys tables."""
    # Users table
    conn.execute("ALTER TABLE users ADD COLUMN metabase_user_id INTEGER")
    conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
    # Sessions table
    conn.execute("ALTER TABLE sessions ADD COLUMN ip_address TEXT")
    conn.execute("ALTER TABLE sessions ADD COLUMN user_agent TEXT")
    # API keys table
    conn.execute("ALTER TABLE api_keys ADD COLUMN key_prefix TEXT NOT NULL DEFAULT ''")
