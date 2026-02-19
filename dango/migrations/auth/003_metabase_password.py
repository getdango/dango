"""dango/migrations/auth/003_metabase_password.py

Add encrypted Metabase password column to users table.

Metabase user sync (TASK-018) stores a randomly generated password per
user, encrypted via SecureTokenStorage (Fernet + OS keychain).  Session
bridging (TASK-019) decrypts the password at login time to create a
Metabase session on behalf of the Dango user.
"""

from __future__ import annotations

import sqlite3

VERSION = 3
DESCRIPTION = "Add encrypted Metabase password column"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add metabase_password_enc TEXT column to users."""
    conn.execute("ALTER TABLE users ADD COLUMN metabase_password_enc TEXT")
