"""dango/auth/lockout.py

Database-backed account lockout functions for brute-force protection.

All functions take ``db_path: Path`` as their first argument (matching the
convention in ``database.py``) and open short-lived connections internally.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from dango.auth.database import _connect
from dango.logging import get_logger

logger = get_logger(__name__)


def record_failed_login(
    db_path: Path,
    email: str,
    *,
    max_attempts: int = 5,
    lockout_minutes: int = 15,
) -> tuple[bool, int]:
    """Record a failed login attempt and lock the account if threshold reached.

    Args:
        db_path: Path to the auth SQLite database.
        email: The email address of the user who failed to log in.
        max_attempts: Number of failures before locking.
        lockout_minutes: How long to lock the account (minutes).

    Returns:
        Tuple of (is_now_locked, remaining_seconds). If the user does not
        exist, returns ``(False, 0)`` to avoid revealing user existence.
    """
    now = datetime.now(timezone.utc)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, failed_login_attempts, locked_until FROM users WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
        if row is None:
            return (False, 0)

        user_id: str = row["id"]
        current_attempts: int = row["failed_login_attempts"]
        locked_until_str: str | None = row["locked_until"]

        # If already locked and not expired, return remaining time
        lockout_expired = False
        if locked_until_str is not None:
            locked_until = datetime.fromisoformat(locked_until_str)
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=timezone.utc)
            if locked_until > now:
                remaining = int((locked_until - now).total_seconds())
                return (True, max(remaining, 1))
            # Lockout expired — reset counter so user gets fresh attempts
            current_attempts = 0
            lockout_expired = True

        new_attempts = current_attempts + 1
        is_locked = new_attempts >= max_attempts

        if is_locked:
            locked_until_dt = now + timedelta(minutes=lockout_minutes)
            conn.execute(
                "UPDATE users SET failed_login_attempts = ?, locked_until = ?, updated_at = ? WHERE id = ?",
                (new_attempts, locked_until_dt.isoformat(), now.isoformat(), user_id),
            )
            conn.commit()
            remaining = int(timedelta(minutes=lockout_minutes).total_seconds())
            logger.warning(
                "account_locked",
                email=email,
                attempts=new_attempts,
                lockout_minutes=lockout_minutes,
            )
            return (True, remaining)

        # Clear stale locked_until when lockout has expired
        if lockout_expired:
            conn.execute(
                "UPDATE users SET failed_login_attempts = ?, locked_until = NULL, updated_at = ? WHERE id = ?",
                (new_attempts, now.isoformat(), user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET failed_login_attempts = ?, updated_at = ? WHERE id = ?",
                (new_attempts, now.isoformat(), user_id),
            )
        conn.commit()
        return (False, 0)
    finally:
        conn.close()


def check_account_locked(db_path: Path, email: str) -> tuple[bool, int]:
    """Check whether an account is currently locked.

    Args:
        db_path: Path to the auth SQLite database.
        email: The email address to check.

    Returns:
        Tuple of (is_locked, remaining_seconds). Returns ``(False, 0)`` for
        unknown emails or expired lockouts.
    """
    now = datetime.now(timezone.utc)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT locked_until FROM users WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
        if row is None:
            return (False, 0)

        locked_until_str: str | None = row["locked_until"]
        if locked_until_str is None:
            return (False, 0)

        locked_until = datetime.fromisoformat(locked_until_str)
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)

        if locked_until > now:
            remaining = int((locked_until - now).total_seconds())
            return (True, max(remaining, 1))

        return (False, 0)
    finally:
        conn.close()


def reset_failed_logins(db_path: Path, email: str) -> None:
    """Reset failed login counter and clear any lockout for a user.

    Called by the login endpoint on successful authentication.

    Args:
        db_path: Path to the auth SQLite database.
        email: The email address whose counters to reset.
    """
    now = datetime.now(timezone.utc)
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, updated_at = ? WHERE email = ?",
            (now.isoformat(), email.strip().lower()),
        )
        conn.commit()
    finally:
        conn.close()


def unlock_account(db_path: Path, email: str) -> bool:
    """Admin unlock — reset counter and clear lockout for a user.

    Args:
        db_path: Path to the auth SQLite database.
        email: The email address of the account to unlock.

    Returns:
        True if a user was found and unlocked, False if the email was not found.
    """
    now = datetime.now(timezone.utc)
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, updated_at = ? WHERE email = ?",
            (now.isoformat(), email.strip().lower()),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
