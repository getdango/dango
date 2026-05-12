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
    cleanup_expired_login_attempts,
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


def _get_login_attempt_row(db_path: Path, key: str) -> dict[str, Any] | None:
    """Read raw login_attempts row for assertions."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT attempts, locked_until FROM login_attempts WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "attempts": row["attempts"],
            "locked_until": row["locked_until"],
        }
    finally:
        conn.close()


@pytest.mark.unit
class TestIPBasedLockout:
    """Tests for IP-based lockout tracking (BUG-235)."""

    def test_unknown_email_locked_after_max_attempts(self, tmp_path: Path) -> None:
        """Unknown email + IP gets locked after max_attempts (no user required)."""
        db_path = _make_db(tmp_path)

        for _ in range(4):
            is_locked, _ = record_failed_login(
                db_path,
                "nobody@example.com",
                client_ip="1.2.3.4",
                max_attempts=5,
            )
            assert is_locked is False

        is_locked, remaining = record_failed_login(
            db_path,
            "nobody@example.com",
            client_ip="1.2.3.4",
            max_attempts=5,
        )
        assert is_locked is True
        assert remaining > 0

    def test_known_email_locked_after_max_attempts(self, tmp_path: Path) -> None:
        """Existing email + IP gets locked after max_attempts (both tables updated)."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        for _ in range(4):
            is_locked, _ = record_failed_login(
                db_path,
                "test@example.com",
                client_ip="1.2.3.4",
                max_attempts=5,
            )
            assert is_locked is False

        is_locked, remaining = record_failed_login(
            db_path,
            "test@example.com",
            client_ip="1.2.3.4",
            max_attempts=5,
        )
        assert is_locked is True
        assert remaining > 0

        # Users table should also be updated for admin visibility
        row = _get_user_row(db_path, "test@example.com")
        assert row["failed_login_attempts"] == 5
        assert row["locked_until"] is not None

    def test_different_ips_independent(self, tmp_path: Path) -> None:
        """Same email from different IPs tracks independently."""
        db_path = _make_db(tmp_path)

        for _ in range(4):
            record_failed_login(
                db_path,
                "test@example.com",
                client_ip="1.1.1.1",
                max_attempts=5,
            )

        # Different IP should start fresh
        is_locked, _ = record_failed_login(
            db_path,
            "test@example.com",
            client_ip="2.2.2.2",
            max_attempts=5,
        )
        assert is_locked is False

    def test_different_emails_same_ip_independent(self, tmp_path: Path) -> None:
        """Same IP with different emails tracks independently."""
        db_path = _make_db(tmp_path)

        for _ in range(4):
            record_failed_login(
                db_path,
                "alice@example.com",
                client_ip="1.2.3.4",
                max_attempts=5,
            )

        # Different email from same IP should start fresh
        is_locked, _ = record_failed_login(
            db_path,
            "bob@example.com",
            client_ip="1.2.3.4",
            max_attempts=5,
        )
        assert is_locked is False

    def test_check_locked_with_ip_unknown_email(self, tmp_path: Path) -> None:
        """check_account_locked returns True for locked IP+unknown email."""
        db_path = _make_db(tmp_path)

        # Lock out via IP-based tracking
        for _ in range(5):
            record_failed_login(
                db_path,
                "nobody@example.com",
                client_ip="1.2.3.4",
                max_attempts=5,
            )

        is_locked, remaining = check_account_locked(
            db_path,
            "nobody@example.com",
            client_ip="1.2.3.4",
        )
        assert is_locked is True
        assert remaining > 0

    def test_reset_clears_ip_entry(self, tmp_path: Path) -> None:
        """reset_failed_logins with client_ip deletes the specific entry."""
        db_path = _make_db(tmp_path)

        for _ in range(3):
            record_failed_login(
                db_path,
                "test@example.com",
                client_ip="1.2.3.4",
                max_attempts=5,
            )

        reset_failed_logins(db_path, "test@example.com", client_ip="1.2.3.4")

        # IP-based entry should be gone
        is_locked, _ = check_account_locked(
            db_path,
            "test@example.com",
            client_ip="1.2.3.4",
        )
        assert is_locked is False

    def test_unlock_clears_all_ip_entries(self, tmp_path: Path) -> None:
        """unlock_account clears ALL IP entries for the email."""
        db_path = _make_db(tmp_path)
        create_user(db_path, _make_user())

        # Lock from two different IPs
        for _ in range(5):
            record_failed_login(
                db_path,
                "test@example.com",
                client_ip="1.1.1.1",
                max_attempts=5,
            )
            record_failed_login(
                db_path,
                "test@example.com",
                client_ip="2.2.2.2",
                max_attempts=5,
            )

        unlock_account(db_path, "test@example.com")

        # Both IP entries should be cleared
        is_locked1, _ = check_account_locked(
            db_path,
            "test@example.com",
            client_ip="1.1.1.1",
        )
        is_locked2, _ = check_account_locked(
            db_path,
            "test@example.com",
            client_ip="2.2.2.2",
        )
        assert is_locked1 is False
        assert is_locked2 is False

    def test_expired_ip_lockout_resets(self, tmp_path: Path) -> None:
        """After IP-based lockout expires, new attempts start fresh."""
        db_path = _make_db(tmp_path)
        from dango.auth.lockout import _make_attempt_key

        key = _make_attempt_key("1.2.3.4", "test@example.com")

        # Insert an expired lockout directly
        past_lockout = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO login_attempts (key, email, attempts, locked_until, updated_at) VALUES (?, ?, ?, ?, ?)",
                (key, "test@example.com", 5, past_lockout, past_lockout),
            )
            conn.commit()
        finally:
            conn.close()

        # Should start fresh (not locked)
        is_locked, _ = record_failed_login(
            db_path,
            "test@example.com",
            client_ip="1.2.3.4",
            max_attempts=5,
        )
        assert is_locked is False

    def test_no_ip_backwards_compatible(self, tmp_path: Path) -> None:
        """Without client_ip, all functions use legacy users-table behaviour."""
        db_path = _make_db(tmp_path)
        # Unknown email without client_ip returns (False, 0) — no error
        is_locked, remaining = record_failed_login(db_path, "nobody@example.com")
        assert is_locked is False
        assert remaining == 0

    def test_cleanup_expired(self, tmp_path: Path) -> None:
        """cleanup_expired_login_attempts removes old entries."""
        db_path = _make_db(tmp_path)
        from dango.auth.lockout import _make_attempt_key

        key = _make_attempt_key("1.2.3.4", "old@example.com")

        # Insert an old entry (48 hours ago)
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO login_attempts (key, email, attempts, locked_until, updated_at) VALUES (?, ?, ?, ?, ?)",
                (key, "old@example.com", 3, None, old_time),
            )
            conn.commit()
        finally:
            conn.close()

        deleted = cleanup_expired_login_attempts(db_path, max_age_hours=24)
        assert deleted == 1

        # Verify entry is gone
        row = _get_login_attempt_row(db_path, key)
        assert row is None

    def test_cleanup_login_attempts_job(self, tmp_path: Path) -> None:
        """cleanup_login_attempts_job wrapper works end-to-end."""
        from dango.auth.lockout import _make_attempt_key, cleanup_login_attempts_job

        # Create auth.db in the expected .dango/ subdirectory
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        db_path = _make_db(dango_dir)
        # _make_db creates at dango_dir/auth.db — move to .dango/auth.db
        # Actually _make_db(dango_dir) already creates dango_dir/auth.db
        # but get_auth_db_path expects tmp_path/.dango/auth.db
        # Since dango_dir IS tmp_path/.dango, pass tmp_path as project_root

        key = _make_attempt_key("1.2.3.4", "old@example.com")

        # Insert an old entry (48 hours ago)
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO login_attempts (key, email, attempts, locked_until, updated_at) VALUES (?, ?, ?, ?, ?)",
                (key, "old@example.com", 3, None, old_time),
            )
            conn.commit()
        finally:
            conn.close()

        # Call the scheduler-compatible wrapper
        cleanup_login_attempts_job(str(tmp_path))

        # Verify entry is gone
        row = _get_login_attempt_row(db_path, key)
        assert row is None
