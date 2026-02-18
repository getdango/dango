"""dango/auth/database.py

Synchronous CRUD operations for users, sessions, and API keys.

All functions take ``db_path: Path`` as their first argument and open
short-lived connections internally. The underlying SQLite database uses
WAL mode and foreign keys.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dango.auth.models import APIKey, Role, Session, User, UserUpdate
from dango.exceptions import UserExistsError, UserNotFoundError

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and foreign keys enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _bool_from_int(value: int) -> bool:
    """Convert SQLite integer (0/1) to Python bool."""
    return bool(value)


def _datetime_from_str(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string to a timezone-aware datetime."""
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_user(row: sqlite3.Row) -> User:
    """Convert a sqlite3.Row to a User model."""
    return User(
        id=row["id"],
        email=row["email"],
        password_hash=row["password_hash"],
        role=Role(row["role"]),
        is_active=_bool_from_int(row["is_active"]),
        totp_secret=row["totp_secret"],
        totp_enabled=_bool_from_int(row["totp_enabled"]),
        recovery_codes=row["recovery_codes"],
        oauth_provider=row["oauth_provider"],
        oauth_id=row["oauth_id"],
        failed_login_attempts=row["failed_login_attempts"],
        locked_until=_datetime_from_str(row["locked_until"]),
        metabase_user_id=row["metabase_user_id"],
        metabase_password_enc=row["metabase_password_enc"],
        must_change_password=_bool_from_int(row["must_change_password"]),
        last_login=_datetime_from_str(row["last_login"]),
        created_at=_datetime_from_str(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_datetime_from_str(row["updated_at"]),  # type: ignore[arg-type]
    )


def _row_to_session(row: sqlite3.Row) -> Session:
    """Convert a sqlite3.Row to a Session model."""
    return Session(
        id=row["id"],
        user_id=row["user_id"],
        token_hash=row["token_hash"],
        is_active=_bool_from_int(row["is_active"]),
        is_partial=_bool_from_int(row["is_partial"]),
        created_at=_datetime_from_str(row["created_at"]),  # type: ignore[arg-type]
        expires_at=_datetime_from_str(row["expires_at"]),  # type: ignore[arg-type]
        last_activity=_datetime_from_str(row["last_activity"]),  # type: ignore[arg-type]
        ip_address=row["ip_address"],
        user_agent=row["user_agent"],
    )


def _row_to_api_key(row: sqlite3.Row) -> APIKey:
    """Convert a sqlite3.Row to an APIKey model."""
    return APIKey(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        key_hash=row["key_hash"],
        key_prefix=row["key_prefix"],
        is_active=_bool_from_int(row["is_active"]),
        created_at=_datetime_from_str(row["created_at"]),  # type: ignore[arg-type]
        last_used_at=_datetime_from_str(row["last_used_at"]),
        expires_at=_datetime_from_str(row["expires_at"]),
    )


def _dt_to_str(dt: datetime | None) -> str | None:
    """Serialize a datetime to ISO 8601 string, or None."""
    if dt is None:
        return None
    return dt.isoformat()


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------


def create_user(db_path: Path, user: User) -> User:
    """Insert a new user. Raises ``UserExistsError`` on duplicate email."""
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO users (
                id, email, password_hash, role, is_active,
                totp_secret, totp_enabled, recovery_codes,
                oauth_provider, oauth_id, failed_login_attempts, locked_until,
                metabase_user_id, metabase_password_enc,
                must_change_password, last_login,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user.id,
                user.email,
                user.password_hash,
                user.role.value,
                int(user.is_active),
                user.totp_secret,
                int(user.totp_enabled),
                user.recovery_codes,
                user.oauth_provider,
                user.oauth_id,
                user.failed_login_attempts,
                _dt_to_str(user.locked_until),
                user.metabase_user_id,
                user.metabase_password_enc,
                int(user.must_change_password),
                _dt_to_str(user.last_login),
                user.created_at.isoformat(),
                user.updated_at.isoformat(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" in str(exc) and "email" in str(exc):
            raise UserExistsError(
                f"A user with email '{user.email}' already exists",
                context={"email": user.email},
            ) from exc
        raise  # pragma: no cover
    finally:
        conn.close()
    return user


def get_user_by_email(db_path: Path, email: str) -> User | None:
    """Look up a user by email address. Returns None if not found."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
        if row is None:
            return None
        return _row_to_user(row)
    finally:
        conn.close()


def get_user_by_id(db_path: Path, user_id: str) -> User | None:
    """Look up a user by ID. Returns None if not found."""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        return _row_to_user(row)
    finally:
        conn.close()


def list_users(db_path: Path, *, active_only: bool = False) -> list[User]:
    """Return all users, optionally filtering to active users only."""
    conn = _connect(db_path)
    try:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM users WHERE is_active = 1 ORDER BY created_at"
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [_row_to_user(row) for row in rows]
    finally:
        conn.close()


def update_user(db_path: Path, user_id: str, updates: UserUpdate) -> User:
    """Apply partial updates to a user.

    Raises:
        UserNotFoundError: If the user does not exist.
        UserExistsError: If the email update conflicts with an existing user.
    """
    changes: dict[str, Any] = updates.model_dump(exclude_unset=True)
    if not changes:
        user = get_user_by_id(db_path, user_id)
        if user is None:
            raise UserNotFoundError(f"User '{user_id}' not found", context={"user_id": user_id})
        return user

    # Convert Python types to SQLite-compatible values
    sql_values: dict[str, Any] = {}
    for key, value in changes.items():
        if value is None:
            sql_values[key] = None
        elif isinstance(value, bool):
            sql_values[key] = int(value)
        elif isinstance(value, datetime):
            sql_values[key] = value.isoformat()
        elif isinstance(value, Role):
            sql_values[key] = value.value
        else:
            sql_values[key] = value

    # Always update updated_at
    sql_values["updated_at"] = datetime.now(timezone.utc).isoformat()

    set_clause = ", ".join(f"{col} = ?" for col in sql_values)
    values = list(sql_values.values()) + [user_id]

    conn = _connect(db_path)
    try:
        try:
            cursor = conn.execute(
                f"UPDATE users SET {set_clause} WHERE id = ?",  # noqa: S608
                values,
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc) and "email" in str(exc):
                raise UserExistsError(
                    f"A user with email '{changes.get('email')}' already exists",
                    context={"email": changes.get("email", "")},
                ) from exc
            raise  # pragma: no cover
        if cursor.rowcount == 0:
            raise UserNotFoundError(f"User '{user_id}' not found", context={"user_id": user_id})
        conn.commit()
    finally:
        conn.close()

    user = get_user_by_id(db_path, user_id)
    assert user is not None  # We just confirmed it exists
    return user


def deactivate_user(db_path: Path, user_id: str) -> User:
    """Deactivate a user account. Raises ``UserNotFoundError`` if not found."""
    return update_user(db_path, user_id, UserUpdate(is_active=False))


def delete_user(db_path: Path, user_id: str) -> None:
    """Hard-delete a user (CASCADE removes sessions and API keys).

    Raises:
        UserNotFoundError: If the user does not exist.
    """
    conn = _connect(db_path)
    try:
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        if cursor.rowcount == 0:
            raise UserNotFoundError(f"User '{user_id}' not found", context={"user_id": user_id})
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


def create_session(db_path: Path, session: Session) -> Session:
    """Insert a new session record."""
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO sessions (
                id, user_id, token_hash, is_active, is_partial,
                created_at, expires_at, last_activity,
                ip_address, user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.user_id,
                session.token_hash,
                int(session.is_active),
                int(session.is_partial),
                session.created_at.isoformat(),
                session.expires_at.isoformat(),
                session.last_activity.isoformat(),
                session.ip_address,
                session.user_agent,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return session


def get_session_by_token(db_path: Path, token_hash: str) -> Session | None:
    """Look up a session by its token hash. Returns None if not found."""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
        if row is None:
            return None
        return _row_to_session(row)
    finally:
        conn.close()


def update_session_activity(db_path: Path, session_id: str) -> None:
    """Update a session's last_activity timestamp to now."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE sessions SET last_activity = ? WHERE id = ?",
            (now, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def invalidate_session(db_path: Path, session_id: str) -> None:
    """Mark a single session as inactive."""
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE sessions SET is_active = 0 WHERE id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def invalidate_all_user_sessions(db_path: Path, user_id: str) -> int:
    """Mark all active sessions for a user as inactive. Returns count."""
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE sessions SET is_active = 0 WHERE user_id = ? AND is_active = 1",
            (user_id,),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def list_user_sessions(db_path: Path, user_id: str, *, active_only: bool = True) -> list[Session]:
    """List sessions for a user, optionally filtering to active only."""
    conn = _connect(db_path)
    try:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND is_active = 1 ORDER BY created_at",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE user_id = ? ORDER BY created_at",
                (user_id,),
            ).fetchall()
        return [_row_to_session(row) for row in rows]
    finally:
        conn.close()


def cleanup_expired_sessions(db_path: Path) -> int:
    """Delete expired sessions. Returns number of rows deleted."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(db_path)
    try:
        cursor = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API Key CRUD
# ---------------------------------------------------------------------------


def create_api_key(db_path: Path, api_key: APIKey) -> APIKey:
    """Insert a new API key record."""
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO api_keys (
                id, user_id, name, key_hash, key_prefix, is_active,
                created_at, last_used_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                api_key.id,
                api_key.user_id,
                api_key.name,
                api_key.key_hash,
                api_key.key_prefix,
                int(api_key.is_active),
                api_key.created_at.isoformat(),
                _dt_to_str(api_key.last_used_at),
                _dt_to_str(api_key.expires_at),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return api_key


def get_api_key_by_hash(db_path: Path, key_hash: str) -> APIKey | None:
    """Look up an API key by its hash. Returns None if not found."""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)).fetchone()
        if row is None:
            return None
        return _row_to_api_key(row)
    finally:
        conn.close()


def list_user_api_keys(db_path: Path, user_id: str, *, active_only: bool = True) -> list[APIKey]:
    """List API keys for a user, optionally filtering to active only."""
    conn = _connect(db_path)
    try:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ? AND is_active = 1 ORDER BY created_at",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at",
                (user_id,),
            ).fetchall()
        return [_row_to_api_key(row) for row in rows]
    finally:
        conn.close()


def revoke_api_key(db_path: Path, key_id: str) -> None:
    """Mark an API key as inactive (revoked)."""
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE api_keys SET is_active = 0 WHERE id = ?", (key_id,))
        conn.commit()
    finally:
        conn.close()
