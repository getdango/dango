"""dango/auth/lockout.py

Database-backed account lockout functions for brute-force protection.

Tracks failed login attempts in two complementary ways:

1. **Users table** (original) — per-user counters visible to admins via
   ``dango auth list-users``.  Only works for existing user accounts.
2. **``login_attempts`` table** (BUG-235) — keyed by SHA-256 of
   ``ip:email``, so both existing and non-existent emails get identical
   lockout behaviour from the same IP address.  Prevents username
   enumeration via lockout-timing side channels.

All functions take ``db_path: Path`` as their first argument (matching the
convention in ``database.py``) and open short-lived connections internally.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dango.auth.database import _connect
from dango.logging import get_logger

logger = get_logger(__name__)


def _make_attempt_key(client_ip: str, email: str) -> str:
    """Build a deterministic lookup key from IP and normalised email."""
    normalized = email.strip().lower()
    return hashlib.sha256(
        f"{client_ip}:{normalized}".encode(),
        usedforsecurity=False,
    ).hexdigest()


# ---------------------------------------------------------------------------
# Record failed login
# ---------------------------------------------------------------------------


def record_failed_login(
    db_path: Path,
    email: str,
    *,
    client_ip: str | None = None,
    max_attempts: int = 5,
    lockout_minutes: int = 15,
) -> tuple[bool, int]:
    """Record a failed login attempt and lock the account if threshold reached.

    Args:
        db_path: Path to the auth SQLite database.
        email: The email address of the failed login attempt.
        client_ip: When provided, track in the ``login_attempts`` table
            (IP-based). When ``None``, fall back to the legacy users-table
            behaviour for backwards compatibility.
        max_attempts: Number of failures before locking.
        lockout_minutes: How long to lock the account (minutes).

    Returns:
        Tuple of ``(is_now_locked, remaining_seconds)``.
    """
    if client_ip is not None:
        return _record_ip_based(
            db_path,
            email,
            client_ip,
            max_attempts=max_attempts,
            lockout_minutes=lockout_minutes,
        )
    return _record_user_based(
        db_path,
        email,
        max_attempts=max_attempts,
        lockout_minutes=lockout_minutes,
    )


def _record_ip_based(
    db_path: Path,
    email: str,
    client_ip: str,
    *,
    max_attempts: int,
    lockout_minutes: int,
) -> tuple[bool, int]:
    """Track a failed attempt keyed by IP+email in ``login_attempts``."""
    now = datetime.now(timezone.utc)
    key = _make_attempt_key(client_ip, email)
    normalized_email = email.strip().lower()
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT attempts, locked_until FROM login_attempts WHERE key = ?",
            (key,),
        ).fetchone()

        current_attempts = 0
        if row is not None:
            locked_until_str: str | None = row["locked_until"]
            # Already locked and not expired — return remaining time
            if locked_until_str is not None:
                locked_until = datetime.fromisoformat(locked_until_str)
                if locked_until.tzinfo is None:
                    locked_until = locked_until.replace(tzinfo=timezone.utc)
                if locked_until > now:
                    remaining = int((locked_until - now).total_seconds())
                    return (True, max(remaining, 1))
                # Lockout expired — reset for fresh attempts
                current_attempts = 0
            else:
                current_attempts = row["attempts"]

        new_attempts = current_attempts + 1
        is_locked = new_attempts >= max_attempts

        locked_until_val: str | None = None
        if is_locked:
            locked_until_val = (now + timedelta(minutes=lockout_minutes)).isoformat()

        conn.execute(
            """
            INSERT INTO login_attempts (key, email, attempts, locked_until, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                attempts = excluded.attempts,
                locked_until = excluded.locked_until,
                updated_at = excluded.updated_at
            """,
            (key, normalized_email, new_attempts, locked_until_val, now.isoformat()),
        )
        conn.commit()

        # Best-effort update of users table for admin visibility
        _update_user_table_best_effort(conn, normalized_email, new_attempts, locked_until_val, now)

        if is_locked:
            remaining = int(timedelta(minutes=lockout_minutes).total_seconds())
            logger.warning(
                "account_locked",
                email=email,
                client_ip=client_ip,
                attempts=new_attempts,
                lockout_minutes=lockout_minutes,
            )
            return (True, remaining)

        return (False, 0)
    finally:
        conn.close()


def _update_user_table_best_effort(
    conn: sqlite3.Connection,
    normalized_email: str,
    attempts: int,
    locked_until_val: str | None,
    now: datetime,
) -> None:
    """Update the users table if the email exists (best-effort, no error on 0 rows)."""
    if locked_until_val is not None:
        conn.execute(
            "UPDATE users SET failed_login_attempts = ?, locked_until = ?, updated_at = ? WHERE email = ?",
            (attempts, locked_until_val, now.isoformat(), normalized_email),
        )
    else:
        conn.execute(
            "UPDATE users SET failed_login_attempts = ?, updated_at = ? WHERE email = ?",
            (attempts, now.isoformat(), normalized_email),
        )
    conn.commit()


def _record_user_based(
    db_path: Path,
    email: str,
    *,
    max_attempts: int,
    lockout_minutes: int,
) -> tuple[bool, int]:
    """Legacy behaviour: track attempts in the users table only."""
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


# ---------------------------------------------------------------------------
# Check account locked
# ---------------------------------------------------------------------------


def check_account_locked(
    db_path: Path,
    email: str,
    *,
    client_ip: str | None = None,
) -> tuple[bool, int]:
    """Check whether an account is currently locked.

    Args:
        db_path: Path to the auth SQLite database.
        email: The email address to check.
        client_ip: When provided, also check the ``login_attempts`` table.

    Returns:
        Tuple of ``(is_locked, remaining_seconds)``. Returns ``(False, 0)``
        for expired lockouts.  When ``client_ip`` is given, the account is
        considered locked if *either* the IP-based or the user-based lockout
        is active.
    """
    now = datetime.now(timezone.utc)

    # Check IP-based lockout first (works for non-existent emails too)
    if client_ip is not None:
        ip_locked, ip_remaining = _check_ip_locked(db_path, client_ip, email, now)
        if ip_locked:
            return (True, ip_remaining)

    # Fall through to user-based check
    return _check_user_locked(db_path, email, now)


def _check_ip_locked(
    db_path: Path,
    client_ip: str,
    email: str,
    now: datetime,
) -> tuple[bool, int]:
    """Check the ``login_attempts`` table for an active lockout."""
    key = _make_attempt_key(client_ip, email)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT locked_until FROM login_attempts WHERE key = ?",
            (key,),
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


def _check_user_locked(
    db_path: Path,
    email: str,
    now: datetime,
) -> tuple[bool, int]:
    """Check the ``users`` table for an active lockout."""
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


# ---------------------------------------------------------------------------
# Reset / unlock
# ---------------------------------------------------------------------------


def reset_failed_logins(
    db_path: Path,
    email: str,
    *,
    client_ip: str | None = None,
) -> None:
    """Reset failed login counter and clear any lockout for a user.

    Called by the login endpoint on successful authentication.

    Args:
        db_path: Path to the auth SQLite database.
        email: The email address whose counters to reset.
        client_ip: When provided, also delete the IP-based entry.
    """
    now = datetime.now(timezone.utc)
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, updated_at = ? WHERE email = ?",
            (now.isoformat(), email.strip().lower()),
        )
        if client_ip is not None:
            key = _make_attempt_key(client_ip, email)
            conn.execute("DELETE FROM login_attempts WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


def unlock_account(db_path: Path, email: str) -> bool:
    """Admin unlock — reset counter and clear lockout for a user.

    Clears both the users-table lockout and *all* IP-based lockouts for
    the given email (so the user can log in from any IP immediately).

    Args:
        db_path: Path to the auth SQLite database.
        email: The email address of the account to unlock.

    Returns:
        True if a user was found and unlocked, False if the email was not found.
    """
    now = datetime.now(timezone.utc)
    normalized = email.strip().lower()
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, updated_at = ? WHERE email = ?",
            (now.isoformat(), normalized),
        )
        # Clear all IP-based lockouts for this email
        conn.execute("DELETE FROM login_attempts WHERE email = ?", (normalized,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_expired_login_attempts(
    db_path: Path,
    *,
    max_age_hours: int = 24,
) -> int:
    """Delete ``login_attempts`` rows older than *max_age_hours*.

    Returns the number of rows deleted.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM login_attempts WHERE updated_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
