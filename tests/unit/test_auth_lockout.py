"""tests/unit/test_auth_lockout.py

Tests for account lockout functions in dango/auth/lockout.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from dango.auth.database import _connect, create_user
from dango.auth.lockout import (
    check_account_locked,
    record_failed_login,
    reset_failed_logins,
    unlock_account,
)
from dango.auth.models import Role, User
from dango.migrations.runner import MigrationRunner


def _make_db(tmp_path: Path) -> Path:
    """Create a fresh auth database by running migrations."""
    db_path = tmp_path / "auth.db"
    migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    runner = MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=migrations_dir)
    runner.apply_pending()
    return db_path


def _make_user(**overrides: Any) -> User:
    """Build a User model with sensible defaults."""
    defaults: dict[str, Any] = {
        "email": "test@example.com",
        "password_hash": "$2b$12$fakehash",
        "role": Role.VIEWER,
    }
    defaults.update(overrides)
    return User(**defaults)


def _get_user_row(db_path: Path, email: str) -> dict[str, Any]:
    """Read raw user row from the database for assertions."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT failed_login_attempts, locked_until FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        assert row is not None
        return {
            "failed_login_attempts": row["failed_login_attempts"],
            "locked_until": row["locked_until"],
        }
    finally:
        conn.close()


@pytest.mark.unit
class TestRecordFailedLogin:
    """Tests for record_failed_login()."""

    def test_increments_counter(self, tmp_path: Path) -> None:
        """Each failed login increments the counter."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        record_failed_login(db_path, "test@example.com")
        row = _get_user_row(db_path, "test@example.com")
        assert row["failed_login_attempts"] == 1
        assert row["locked_until"] is None

    def test_locks_after_max_attempts(self, tmp_path: Path) -> None:
        """Account locks when failed attempts reach the threshold."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        for _ in range(4):
            is_locked, _ = record_failed_login(db_path, "test@example.com", max_attempts=5)
            assert is_locked is False

        is_locked, remaining = record_failed_login(db_path, "test@example.com", max_attempts=5)
        assert is_locked is True
        assert remaining > 0

        row = _get_user_row(db_path, "test@example.com")
        assert row["failed_login_attempts"] == 5
        assert row["locked_until"] is not None

    def test_returns_remaining_seconds(self, tmp_path: Path) -> None:
        """Locked account returns approximate remaining lockout seconds."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        # Lock the account with 15-minute lockout
        for _ in range(5):
            record_failed_login(db_path, "test@example.com", max_attempts=5, lockout_minutes=15)

        is_locked, remaining = record_failed_login(db_path, "test@example.com", max_attempts=5)
        assert is_locked is True
        # Should be close to 15 * 60 = 900 seconds
        assert 800 <= remaining <= 900

    def test_unknown_email_safe(self, tmp_path: Path) -> None:
        """Unknown email returns (False, 0) without error."""
        db_path = _make_db(tmp_path)
        is_locked, remaining = record_failed_login(db_path, "nobody@example.com")
        assert is_locked is False
        assert remaining == 0

    def test_already_locked_returns_remaining(self, tmp_path: Path) -> None:
        """Already-locked account returns remaining time without incrementing."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        # Lock the account
        for _ in range(5):
            record_failed_login(db_path, "test@example.com", max_attempts=5)

        row_before = _get_user_row(db_path, "test@example.com")

        # Subsequent attempt should not increment
        is_locked, remaining = record_failed_login(db_path, "test@example.com", max_attempts=5)
        assert is_locked is True
        assert remaining > 0

        row_after = _get_user_row(db_path, "test@example.com")
        assert row_after["failed_login_attempts"] == row_before["failed_login_attempts"]

    def test_custom_max_attempts(self, tmp_path: Path) -> None:
        """Custom max_attempts threshold is respected."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        for _ in range(2):
            is_locked, _ = record_failed_login(db_path, "test@example.com", max_attempts=3)
            assert is_locked is False

        is_locked, _ = record_failed_login(db_path, "test@example.com", max_attempts=3)
        assert is_locked is True

    def test_custom_lockout_minutes(self, tmp_path: Path) -> None:
        """Custom lockout_minutes determines the lockout duration."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        for _ in range(5):
            record_failed_login(db_path, "test@example.com", max_attempts=5, lockout_minutes=30)

        is_locked, remaining = record_failed_login(db_path, "test@example.com", lockout_minutes=30)
        assert is_locked is True
        # Should be close to 30 * 60 = 1800 seconds
        assert 1700 <= remaining <= 1800

    def test_expired_lockout_allows_new_attempts(self, tmp_path: Path) -> None:
        """After lockout expires, new failed attempts start fresh counting."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        # Set locked_until in the past
        past_lockout = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn = _connect(db_path)
        try:
            conn.execute(
                "UPDATE users SET failed_login_attempts = 5, locked_until = ? WHERE email = ?",
                (past_lockout, "test@example.com"),
            )
            conn.commit()
        finally:
            conn.close()

        # Next failure should reset counter and start fresh (not return locked)
        is_locked, _ = record_failed_login(db_path, "test@example.com", max_attempts=5)
        assert is_locked is False

        row = _get_user_row(db_path, "test@example.com")
        assert row["failed_login_attempts"] == 1
        assert row["locked_until"] is None  # stale locked_until must be cleared

    def test_relocks_after_expired_lockout(self, tmp_path: Path) -> None:
        """Account can be locked again after a previous lockout expires."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        # Set an expired lockout
        past_lockout = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn = _connect(db_path)
        try:
            conn.execute(
                "UPDATE users SET failed_login_attempts = 5, locked_until = ? WHERE email = ?",
                (past_lockout, "test@example.com"),
            )
            conn.commit()
        finally:
            conn.close()

        # Accumulate fresh failures — should eventually re-lock
        for i in range(4):
            is_locked, _ = record_failed_login(db_path, "test@example.com", max_attempts=5)
            assert is_locked is False
            row = _get_user_row(db_path, "test@example.com")
            assert row["failed_login_attempts"] == i + 1

        is_locked, remaining = record_failed_login(db_path, "test@example.com", max_attempts=5)
        assert is_locked is True
        assert remaining > 0


@pytest.mark.unit
class TestCheckAccountLocked:
    """Tests for check_account_locked()."""

    def test_unlocked_by_default(self, tmp_path: Path) -> None:
        """A fresh user is not locked."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        is_locked, remaining = check_account_locked(db_path, "test@example.com")
        assert is_locked is False
        assert remaining == 0

    def test_locked_returns_true(self, tmp_path: Path) -> None:
        """A locked account returns True with remaining seconds."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        future_lockout = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        conn = _connect(db_path)
        try:
            conn.execute(
                "UPDATE users SET locked_until = ? WHERE email = ?",
                (future_lockout, "test@example.com"),
            )
            conn.commit()
        finally:
            conn.close()

        is_locked, remaining = check_account_locked(db_path, "test@example.com")
        assert is_locked is True
        assert remaining > 0

    def test_expired_lockout_returns_false(self, tmp_path: Path) -> None:
        """An expired lockout returns False."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        past_lockout = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn = _connect(db_path)
        try:
            conn.execute(
                "UPDATE users SET locked_until = ? WHERE email = ?",
                (past_lockout, "test@example.com"),
            )
            conn.commit()
        finally:
            conn.close()

        is_locked, remaining = check_account_locked(db_path, "test@example.com")
        assert is_locked is False
        assert remaining == 0

    def test_unknown_email_returns_false(self, tmp_path: Path) -> None:
        """Unknown email returns (False, 0)."""
        db_path = _make_db(tmp_path)
        is_locked, remaining = check_account_locked(db_path, "nobody@example.com")
        assert is_locked is False
        assert remaining == 0


@pytest.mark.unit
class TestResetFailedLogins:
    """Tests for reset_failed_logins()."""

    def test_resets_counter_and_lockout(self, tmp_path: Path) -> None:
        """Resets both failed_login_attempts and locked_until."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        # Lock the account first
        for _ in range(5):
            record_failed_login(db_path, "test@example.com", max_attempts=5)

        row = _get_user_row(db_path, "test@example.com")
        assert row["failed_login_attempts"] == 5
        assert row["locked_until"] is not None

        reset_failed_logins(db_path, "test@example.com")

        row = _get_user_row(db_path, "test@example.com")
        assert row["failed_login_attempts"] == 0
        assert row["locked_until"] is None

    def test_no_error_for_unknown_email(self, tmp_path: Path) -> None:
        """Resetting an unknown email is a no-op (no error)."""
        db_path = _make_db(tmp_path)
        reset_failed_logins(db_path, "nobody@example.com")


@pytest.mark.unit
class TestUnlockAccount:
    """Tests for unlock_account()."""

    def test_clears_lockout_and_counter(self, tmp_path: Path) -> None:
        """Unlocking clears both the counter and the lockout timestamp."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        for _ in range(5):
            record_failed_login(db_path, "test@example.com", max_attempts=5)

        result = unlock_account(db_path, "test@example.com")
        assert result is True

        row = _get_user_row(db_path, "test@example.com")
        assert row["failed_login_attempts"] == 0
        assert row["locked_until"] is None

        is_locked, _ = check_account_locked(db_path, "test@example.com")
        assert is_locked is False

    def test_returns_false_for_unknown_email(self, tmp_path: Path) -> None:
        """Returns False when the email is not found."""
        db_path = _make_db(tmp_path)
        result = unlock_account(db_path, "nobody@example.com")
        assert result is False
