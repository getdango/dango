"""dango/auth/totp_guard.py

Persistent brute-force and replay protection for TOTP and recovery-code verification.

State lives in the ``login_attempts`` table of ``auth.db`` (no schema change), so it survives
restarts, is shared by every worker and is not reset by a password login:

* ``sha256("totp:" + user_id)`` counts failed codes for the account; each full set of
  ``max_failures`` failures locks verification, and every further lock lasts twice as long
  (capped at 12 hours). Only a good code, an admin unlock or 24 hours without failures clear it.
* ``sha256("totp-step:" + user_id)`` keeps the last accepted time-step in ``attempts``; a code
  of that step or an earlier one is refused, so an observed code cannot be used twice.

Rows carry the user's email, so ``unlock_account`` (admin unlock) clears them too.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyotp

from dango.auth.database import _connect

TOTP_MAX_FAILURES = 5
_MAX_LOCK_MINUTES = 12 * 60


def _key(prefix: str, user_id: str) -> str:
    return hashlib.sha256(f"{prefix}:{user_id}".encode(), usedforsecurity=False).hexdigest()


def totp_lock_remaining(db_path: Path, user_id: str) -> int:
    """Seconds until TOTP verification is allowed again for the account; 0 when it is."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT locked_until FROM login_attempts WHERE key = ?", (_key("totp", user_id),)
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["locked_until"] is None:
        return 0
    locked_until = datetime.fromisoformat(row["locked_until"])
    remaining = (locked_until - datetime.now(timezone.utc)).total_seconds()
    return max(int(remaining), 1) if remaining > 0 else 0


def record_totp_failure(
    db_path: Path,
    user_id: str,
    email: str,
    *,
    max_failures: int = TOTP_MAX_FAILURES,
    lockout_minutes: int = 15,
) -> int:
    """Count one failed code; return the lock's seconds when this failure locked the account."""
    now = datetime.now(timezone.utc)
    key = _key("totp", user_id)
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT attempts FROM login_attempts WHERE key = ?", (key,)).fetchone()
        attempts = (row["attempts"] if row is not None else 0) + 1
        locked_until: str | None = None
        seconds = 0
        if attempts % max_failures == 0:
            minutes = min(lockout_minutes * 2 ** (attempts // max_failures - 1), _MAX_LOCK_MINUTES)
            locked_until = (now + timedelta(minutes=minutes)).isoformat()
            seconds = minutes * 60
        conn.execute(
            """
            INSERT INTO login_attempts (key, email, attempts, locked_until, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                attempts = excluded.attempts,
                locked_until = COALESCE(excluded.locked_until, login_attempts.locked_until),
                updated_at = excluded.updated_at
            """,
            (key, email.strip().lower(), attempts, locked_until, now.isoformat()),
        )
        conn.commit()
        return seconds
    finally:
        conn.close()


def reset_totp_failures(db_path: Path, user_id: str) -> None:
    """Forget the failure count after a good code."""
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM login_attempts WHERE key = ?", (_key("totp", user_id),))
        conn.commit()
    finally:
        conn.close()


def matching_step(secret: str, code: str, *, now: float | None = None) -> int | None:
    """The time-step (within one step either side of now) whose code equals *code*, or None."""
    stripped = code.strip() if code else ""
    if not secret or not stripped.isdigit() or len(stripped) != 6:
        return None
    totp = pyotp.TOTP(secret)
    current = totp.timecode(
        datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    )
    for step in (current - 1, current, current + 1):
        if hmac.compare_digest(totp.generate_otp(step), stripped):
            return step
    return None


def accept_totp_code(db_path: Path, user_id: str, email: str, secret: str, code: str) -> bool:
    """Verify *code* once: a valid code of a time-step not yet used is accepted and recorded.

    The step is stored with a conditional upsert, so two simultaneous submissions of the same
    code cannot both succeed.
    """
    step = matching_step(secret, code)
    if step is None:
        return False
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO login_attempts (key, email, attempts, locked_until, updated_at)
            VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(key) DO UPDATE SET
                attempts = excluded.attempts,
                updated_at = excluded.updated_at
            WHERE login_attempts.attempts < excluded.attempts
            """,
            (
                _key("totp-step", user_id),
                email.strip().lower(),
                step,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()
