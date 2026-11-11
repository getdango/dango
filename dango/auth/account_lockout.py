"""dango/auth/account_lockout.py

Account-wide failed-login counter, independent of the client address.

``lockout.py`` counts failures per ``ip:email``, so guessing spread over many addresses never
reaches the per-address threshold. This module keeps one more row per email in the
``login_attempts`` table (key ``sha256("account:" + email)``) that every failure increments, and
locks the account once it passes ``ACCOUNT_ATTEMPT_FACTOR`` times the per-address threshold.
Unknown emails get the same row, so the lock reveals nothing about which accounts exist.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

#: The account locks after this many times the per-address ``max_attempts`` failures.
ACCOUNT_ATTEMPT_FACTOR = 4


def account_key(email: str) -> str:
    """Row key of the account-wide counter for *email*."""
    return hashlib.sha256(
        f"account:{email.strip().lower()}".encode(), usedforsecurity=False
    ).hexdigest()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def record_account_failure(
    conn: sqlite3.Connection,
    email: str,
    *,
    max_attempts: int,
    lockout_minutes: int,
    now: datetime,
) -> tuple[int, str | None]:
    """Count one failure for the account; return ``(total, locked_until_iso_or_None)``.

    A lock that has expired starts a fresh count; a lock still in force is kept as it is.
    """
    key = account_key(email)
    row = conn.execute(
        "SELECT attempts, locked_until FROM login_attempts WHERE key = ?", (key,)
    ).fetchone()
    attempts = 0
    if row is not None:
        attempts = row["attempts"]
        if row["locked_until"] is not None:
            if _parse(row["locked_until"]) > now:
                return attempts, row["locked_until"]
            attempts = 0  # the lock ran out: count afresh

    attempts += 1
    locked_until: str | None = None
    if attempts >= max_attempts * ACCOUNT_ATTEMPT_FACTOR:
        locked_until = (now + timedelta(minutes=lockout_minutes)).isoformat()
    conn.execute(
        """
        INSERT INTO login_attempts (key, email, attempts, locked_until, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            attempts = excluded.attempts,
            locked_until = excluded.locked_until,
            updated_at = excluded.updated_at
        """,
        (key, email.strip().lower(), attempts, locked_until, now.isoformat()),
    )
    conn.commit()
    return attempts, locked_until


def account_lock_remaining(conn: sqlite3.Connection, email: str, now: datetime) -> int:
    """Seconds left on the account-wide lock, 0 when there is none."""
    row = conn.execute(
        "SELECT locked_until FROM login_attempts WHERE key = ?", (account_key(email),)
    ).fetchone()
    if row is None or row["locked_until"] is None:
        return 0
    locked_until = _parse(row["locked_until"])
    if locked_until <= now:
        return 0
    return max(int((locked_until - now).total_seconds()), 1)


def clear_account_failures(conn: sqlite3.Connection, email: str) -> None:
    """Forget the account-wide count (successful sign-in); the caller commits."""
    conn.execute("DELETE FROM login_attempts WHERE key = ?", (account_key(email),))
