"""tests/unit/test_auth_sessions.py

Tests for session lifecycle management in dango/auth/sessions.py.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dango.auth import database as db
from dango.auth.models import Role, User
from dango.auth.security import hash_token
from dango.auth.sessions import (
    DEFAULT_IDLE_TIMEOUT_MINUTES,
    DEFAULT_PARTIAL_SESSION_TIMEOUT_MINUTES,
    DEFAULT_SESSION_MAX_DAYS,
    cleanup_expired_sessions,
    create_session,
    invalidate_all_sessions,
    invalidate_session,
    validate_partial_session,
    validate_session,
)
from dango.migrations.runner import MigrationRunner


def _make_db(tmp_path: Path) -> Path:
    """Create a fresh auth database by running the migration."""
    db_path = tmp_path / "auth.db"
    migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    runner = MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=migrations_dir)
    runner.apply_pending()
    return db_path


def _make_user(**overrides: Any) -> User:
    """Build a User model with sensible defaults, applying overrides."""
    defaults: dict[str, Any] = {
        "email": "test@example.com",
        "password_hash": "$2b$12$fakehash",
        "role": Role.VIEWER,
    }
    defaults.update(overrides)
    return User(**defaults)


def _setup_user(db_path: Path, **overrides: Any) -> User:
    """Create and persist a user, returning the model."""
    user = _make_user(**overrides)
    db.create_user(db_path, user)
    return user


@pytest.mark.unit
class TestCreateSession:
    """Tests for create_session()."""

    def test_returns_raw_token_and_session(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id)
        assert isinstance(raw_token, str)
        assert len(raw_token) > 0
        assert session.user_id == user.id

    def test_token_hash_matches(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id)
        assert session.token_hash == hash_token(raw_token)

    def test_default_expiry(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        _, session = create_session(db_path, user.id)
        expected = session.created_at + timedelta(days=DEFAULT_SESSION_MAX_DAYS)
        assert abs((session.expires_at - expected).total_seconds()) < 2

    def test_custom_expiry(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        _, session = create_session(db_path, user.id, session_max_days=7)
        expected = session.created_at + timedelta(days=7)
        assert abs((session.expires_at - expected).total_seconds()) < 2

    def test_ip_and_user_agent_stored(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        _, session = create_session(
            db_path, user.id, ip_address="10.0.0.1", user_agent="TestAgent/1.0"
        )
        assert session.ip_address == "10.0.0.1"
        assert session.user_agent == "TestAgent/1.0"

    def test_is_active_by_default(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        _, session = create_session(db_path, user.id)
        assert session.is_active is True

    def test_partial_session_short_expiry(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        _, session = create_session(db_path, user.id, is_partial=True)
        expected = session.created_at + timedelta(minutes=DEFAULT_PARTIAL_SESSION_TIMEOUT_MINUTES)
        assert abs((session.expires_at - expected).total_seconds()) < 2
        assert session.is_partial is True

    def test_partial_session_custom_timeout(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        _, session = create_session(db_path, user.id, is_partial=True, partial_timeout_minutes=2)
        expected = session.created_at + timedelta(minutes=2)
        assert abs((session.expires_at - expected).total_seconds()) < 2

    def test_session_persisted_in_db(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id)
        fetched = db.get_session_by_token(db_path, hash_token(raw_token))
        assert fetched is not None
        assert fetched.id == session.id


@pytest.mark.unit
class TestValidateSession:
    """Tests for validate_session()."""

    def test_happy_path_returns_user(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, _ = create_session(db_path, user.id)
        result = validate_session(db_path, raw_token)
        assert result is not None
        assert result.id == user.id

    def test_sliding_window_updates_last_activity(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id)
        original_activity = session.last_activity
        time.sleep(0.05)
        validate_session(db_path, raw_token)
        fetched = db.get_session_by_token(db_path, session.token_hash)
        assert fetched is not None
        assert fetched.last_activity >= original_activity

    def test_invalid_token_returns_none(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        assert validate_session(db_path, "totally-bogus-token") is None

    def test_inactive_session_returns_none(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id)
        db.invalidate_session(db_path, session.id)
        assert validate_session(db_path, raw_token) is None

    def test_partial_session_rejected(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, _ = create_session(db_path, user.id, is_partial=True)
        assert validate_session(db_path, raw_token) is None

    def test_absolute_timeout_invalidates(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id)
        past_expiry = session.expires_at + timedelta(seconds=1)
        with patch("dango.auth.sessions.datetime") as mock_dt:
            mock_dt.now.return_value = past_expiry
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = validate_session(db_path, raw_token)
        assert result is None
        fetched = db.get_session_by_token(db_path, session.token_hash)
        assert fetched is not None
        assert fetched.is_active is False

    def test_idle_timeout_default_invalidates(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id)
        past_idle = session.last_activity + timedelta(minutes=DEFAULT_IDLE_TIMEOUT_MINUTES + 1)
        with patch("dango.auth.sessions.datetime") as mock_dt:
            mock_dt.now.return_value = past_idle
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = validate_session(db_path, raw_token)
        assert result is None
        fetched = db.get_session_by_token(db_path, session.token_hash)
        assert fetched is not None
        assert fetched.is_active is False

    def test_idle_timeout_custom(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id)
        past_idle = session.last_activity + timedelta(minutes=11)
        with patch("dango.auth.sessions.datetime") as mock_dt:
            mock_dt.now.return_value = past_idle
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = validate_session(db_path, raw_token, idle_timeout_minutes=10)
        assert result is None

    def test_deactivated_user_returns_none(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, _ = create_session(db_path, user.id)
        db.deactivate_user(db_path, user.id)
        assert validate_session(db_path, raw_token) is None

    def test_orphaned_session_returns_none(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, _ = create_session(db_path, user.id)
        db.delete_user(db_path, user.id)
        assert validate_session(db_path, raw_token) is None

    def test_multiple_concurrent_sessions(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        token1, _ = create_session(db_path, user.id)
        token2, _ = create_session(db_path, user.id)
        assert validate_session(db_path, token1) is not None
        assert validate_session(db_path, token2) is not None

    def test_within_idle_timeout_succeeds(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id)
        within_idle = session.last_activity + timedelta(minutes=DEFAULT_IDLE_TIMEOUT_MINUTES - 1)
        with patch("dango.auth.sessions.datetime") as mock_dt:
            mock_dt.now.return_value = within_idle
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = validate_session(db_path, raw_token)
        assert result is not None
        assert result.id == user.id


@pytest.mark.unit
class TestValidatePartialSession:
    """Tests for validate_partial_session()."""

    def test_happy_path(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, _ = create_session(db_path, user.id, is_partial=True)
        result = validate_partial_session(db_path, raw_token)
        assert result is not None
        assert result.id == user.id

    def test_rejects_non_partial(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, _ = create_session(db_path, user.id, is_partial=False)
        assert validate_partial_session(db_path, raw_token) is None

    def test_expired_returns_none(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id, is_partial=True)
        past_expiry = session.expires_at + timedelta(seconds=1)
        with patch("dango.auth.sessions.datetime") as mock_dt:
            mock_dt.now.return_value = past_expiry
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = validate_partial_session(db_path, raw_token)
        assert result is None

    def test_inactive_returns_none(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id, is_partial=True)
        db.invalidate_session(db_path, session.id)
        assert validate_partial_session(db_path, raw_token) is None

    def test_does_not_update_last_activity(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id, is_partial=True)
        original = session.last_activity
        time.sleep(0.05)
        validate_partial_session(db_path, raw_token)
        fetched = db.get_session_by_token(db_path, session.token_hash)
        assert fetched is not None
        assert abs((fetched.last_activity - original).total_seconds()) < 1


@pytest.mark.unit
class TestInvalidateSession:
    """Tests for invalidate_session()."""

    def test_invalidates_target(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_token, session = create_session(db_path, user.id)
        invalidate_session(db_path, session.id)
        assert validate_session(db_path, raw_token) is None

    def test_leaves_others_untouched(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        token1, session1 = create_session(db_path, user.id)
        token2, _ = create_session(db_path, user.id)
        invalidate_session(db_path, session1.id)
        assert validate_session(db_path, token1) is None
        assert validate_session(db_path, token2) is not None


@pytest.mark.unit
class TestInvalidateAllSessions:
    """Tests for invalidate_all_sessions()."""

    def test_invalidates_all_for_user(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        token1, _ = create_session(db_path, user.id)
        token2, _ = create_session(db_path, user.id)
        invalidate_all_sessions(db_path, user.id)
        assert validate_session(db_path, token1) is None
        assert validate_session(db_path, token2) is None

    def test_returns_count(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        create_session(db_path, user.id)
        create_session(db_path, user.id)
        count = invalidate_all_sessions(db_path, user.id)
        assert count == 2

    def test_other_users_unaffected(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user1 = _setup_user(db_path, email="u1@example.com")
        user2 = _setup_user(db_path, email="u2@example.com")
        create_session(db_path, user1.id)
        token2, _ = create_session(db_path, user2.id)
        invalidate_all_sessions(db_path, user1.id)
        assert validate_session(db_path, token2) is not None


@pytest.mark.unit
class TestCleanupExpiredSessions:
    """Tests for cleanup_expired_sessions()."""

    def test_deletes_expired(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        _, session = create_session(db_path, user.id, is_partial=True, partial_timeout_minutes=0)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE id = ?",
            (past.isoformat(), session.id),
        )
        conn.commit()
        conn.close()
        count = cleanup_expired_sessions(db_path)
        assert count == 1
        assert db.get_session_by_token(db_path, session.token_hash) is None

    def test_keeps_active(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        _, session = create_session(db_path, user.id)
        count = cleanup_expired_sessions(db_path)
        assert count == 0
        assert db.get_session_by_token(db_path, session.token_hash) is not None
