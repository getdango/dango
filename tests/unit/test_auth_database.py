"""tests/unit/test_auth_database.py

Tests for auth CRUD operations in dango/auth/database.py.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from dango.auth.database import (
    cleanup_expired_sessions,
    create_api_key,
    create_session,
    create_user,
    deactivate_user,
    delete_user,
    get_api_key_by_hash,
    get_session_by_token,
    get_user_by_email,
    get_user_by_id,
    invalidate_all_user_sessions,
    invalidate_session,
    list_user_api_keys,
    list_user_sessions,
    list_users,
    revoke_api_key,
    update_session_activity,
    update_user,
)
from dango.auth.models import APIKey, Role, Session, User, UserUpdate
from dango.exceptions import UserExistsError, UserNotFoundError
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


@pytest.mark.unit
class TestCreateUser:
    """Tests for create_user()."""

    def test_round_trip(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        created = create_user(db, user)
        assert created.id == user.id
        assert created.email == "test@example.com"

        fetched = get_user_by_id(db, user.id)
        assert fetched is not None
        assert fetched.email == user.email
        assert fetched.password_hash == user.password_hash
        assert fetched.role is Role.VIEWER
        assert fetched.is_active is True

    def test_duplicate_email_raises(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        create_user(db, _make_user(email="dup@example.com"))
        with pytest.raises(UserExistsError, match="already exists"):
            create_user(db, _make_user(email="dup@example.com"))

    def test_preserves_all_fields(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        now = datetime.now(timezone.utc)
        user = User(
            email="full@example.com",
            password_hash="hash",
            role=Role.ADMIN,
            is_active=False,
            totp_secret="secret",
            totp_enabled=True,
            recovery_codes="r1,r2",
            oauth_provider="google",
            oauth_id="g-123",
            failed_login_attempts=5,
            locked_until=now,
        )
        create_user(db, user)
        fetched = get_user_by_id(db, user.id)
        assert fetched is not None
        assert fetched.role is Role.ADMIN
        assert fetched.is_active is False
        assert fetched.totp_secret == "secret"
        assert fetched.totp_enabled is True
        assert fetched.recovery_codes == "r1,r2"
        assert fetched.oauth_provider == "google"
        assert fetched.oauth_id == "g-123"
        assert fetched.failed_login_attempts == 5
        assert fetched.locked_until is not None


@pytest.mark.unit
class TestGetUser:
    """Tests for get_user_by_email() and get_user_by_id()."""

    def test_by_email_found(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user(email="find@example.com")
        create_user(db, user)
        found = get_user_by_email(db, "find@example.com")
        assert found is not None
        assert found.id == user.id

    def test_by_email_not_found(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        assert get_user_by_email(db, "nobody@example.com") is None

    def test_by_email_case_insensitive(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user(email="case@example.com")
        create_user(db, user)
        found = get_user_by_email(db, "  CASE@EXAMPLE.COM  ")
        assert found is not None
        assert found.id == user.id

    def test_by_id_found(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        found = get_user_by_id(db, user.id)
        assert found is not None
        assert found.email == user.email

    def test_by_id_not_found(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        assert get_user_by_id(db, "nonexistent-id") is None


@pytest.mark.unit
class TestListUsers:
    """Tests for list_users()."""

    def test_empty(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        assert list_users(db) == []

    def test_multiple_users(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        create_user(db, _make_user(email="a@example.com"))
        create_user(db, _make_user(email="b@example.com"))
        create_user(db, _make_user(email="c@example.com"))
        users = list_users(db)
        assert len(users) == 3

    def test_active_only_filter(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        create_user(db, _make_user(email="active@example.com"))
        inactive = _make_user(email="inactive@example.com")
        create_user(db, inactive)
        deactivate_user(db, inactive.id)

        all_users = list_users(db)
        assert len(all_users) == 2
        active_users = list_users(db, active_only=True)
        assert len(active_users) == 1
        assert active_users[0].email == "active@example.com"


@pytest.mark.unit
class TestUpdateUser:
    """Tests for update_user()."""

    def test_partial_update(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        updated = update_user(db, user.id, UserUpdate(role=Role.EDITOR))
        assert updated.role is Role.EDITOR
        assert updated.email == user.email  # unchanged

    def test_email_update(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        updated = update_user(db, user.id, UserUpdate(email="new@example.com"))
        assert updated.email == "new@example.com"

    def test_email_conflict_raises(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        create_user(db, _make_user(email="first@example.com"))
        user2 = _make_user(email="second@example.com")
        create_user(db, user2)
        with pytest.raises(UserExistsError, match="already exists"):
            update_user(db, user2.id, UserUpdate(email="first@example.com"))

    def test_not_found_raises(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        with pytest.raises(UserNotFoundError, match="not found"):
            update_user(db, "ghost", UserUpdate(role=Role.ADMIN))

    def test_empty_update_returns_user(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        result = update_user(db, user.id, UserUpdate())
        assert result.id == user.id

    def test_empty_update_not_found_raises(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        with pytest.raises(UserNotFoundError):
            update_user(db, "ghost", UserUpdate())

    def test_updates_updated_at(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        original_updated = user.updated_at
        updated = update_user(db, user.id, UserUpdate(role=Role.ADMIN))
        assert updated.updated_at >= original_updated


@pytest.mark.unit
class TestDeactivateUser:
    """Tests for deactivate_user()."""

    def test_deactivates(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        result = deactivate_user(db, user.id)
        assert result.is_active is False

    def test_not_found_raises(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        with pytest.raises(UserNotFoundError):
            deactivate_user(db, "ghost")


@pytest.mark.unit
class TestDeleteUser:
    """Tests for delete_user()."""

    def test_hard_delete(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        delete_user(db, user.id)
        assert get_user_by_id(db, user.id) is None

    def test_cascade_deletes_sessions(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        session = Session(
            user_id=user.id,
            token_hash="tok1",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        create_session(db, session)
        delete_user(db, user.id)
        assert get_session_by_token(db, "tok1") is None

    def test_cascade_deletes_api_keys(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        key = APIKey(user_id=user.id, name="test-key", key_hash="kh1")
        create_api_key(db, key)
        delete_user(db, user.id)
        assert get_api_key_by_hash(db, "kh1") is None

    def test_not_found_raises(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        with pytest.raises(UserNotFoundError):
            delete_user(db, "ghost")


@pytest.mark.unit
class TestSessionCRUD:
    """Tests for session CRUD operations."""

    def _make_session(self, user_id: str, token_hash: str = "hash1", **kw: Any) -> Session:
        defaults: dict[str, Any] = {
            "user_id": user_id,
            "token_hash": token_hash,
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        defaults.update(kw)
        return Session(**defaults)

    def test_create_and_get(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        session = self._make_session(user.id)
        created = create_session(db, session)
        assert created.id == session.id

        fetched = get_session_by_token(db, session.token_hash)
        assert fetched is not None
        assert fetched.user_id == user.id
        assert fetched.is_active is True

    def test_get_nonexistent_returns_none(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        assert get_session_by_token(db, "nonexistent") is None

    def test_update_activity(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        session = self._make_session(user.id)
        create_session(db, session)
        original_activity = session.last_activity

        update_session_activity(db, session.id)
        fetched = get_session_by_token(db, session.token_hash)
        assert fetched is not None
        assert fetched.last_activity >= original_activity

    def test_invalidate_session(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        session = self._make_session(user.id)
        create_session(db, session)
        invalidate_session(db, session.id)

        fetched = get_session_by_token(db, session.token_hash)
        assert fetched is not None
        assert fetched.is_active is False

    def test_invalidate_all_user_sessions(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        s1 = self._make_session(user.id, token_hash="t1")
        s2 = self._make_session(user.id, token_hash="t2")
        create_session(db, s1)
        create_session(db, s2)

        count = invalidate_all_user_sessions(db, user.id)
        assert count == 2
        sessions = list_user_sessions(db, user.id, active_only=True)
        assert len(sessions) == 0

    def test_list_user_sessions(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        s1 = self._make_session(user.id, token_hash="t1")
        s2 = self._make_session(user.id, token_hash="t2")
        create_session(db, s1)
        create_session(db, s2)
        invalidate_session(db, s1.id)

        active = list_user_sessions(db, user.id, active_only=True)
        assert len(active) == 1
        assert active[0].token_hash == "t2"

        all_sessions = list_user_sessions(db, user.id, active_only=False)
        assert len(all_sessions) == 2

    def test_cleanup_expired_sessions(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)

        # Create one expired and one valid session
        expired = self._make_session(
            user.id,
            token_hash="expired",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        valid = self._make_session(
            user.id,
            token_hash="valid",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        create_session(db, expired)
        create_session(db, valid)

        count = cleanup_expired_sessions(db)
        assert count == 1
        assert get_session_by_token(db, "expired") is None
        assert get_session_by_token(db, "valid") is not None


@pytest.mark.unit
class TestAPIKeyCRUD:
    """Tests for API key CRUD operations."""

    def _make_api_key(self, user_id: str, **kw: Any) -> APIKey:
        defaults: dict[str, Any] = {
            "user_id": user_id,
            "name": "Test Key",
            "key_hash": "keyhash123",
        }
        defaults.update(kw)
        return APIKey(**defaults)

    def test_create_and_get(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        key = self._make_api_key(user.id)
        created = create_api_key(db, key)
        assert created.id == key.id

        fetched = get_api_key_by_hash(db, key.key_hash)
        assert fetched is not None
        assert fetched.name == "Test Key"
        assert fetched.user_id == user.id
        assert fetched.is_active is True

    def test_get_nonexistent_returns_none(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        assert get_api_key_by_hash(db, "nonexistent") is None

    def test_list_user_api_keys(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        k1 = self._make_api_key(user.id, key_hash="kh1", name="Key 1")
        k2 = self._make_api_key(user.id, key_hash="kh2", name="Key 2")
        create_api_key(db, k1)
        create_api_key(db, k2)
        revoke_api_key(db, k1.id)

        active = list_user_api_keys(db, user.id, active_only=True)
        assert len(active) == 1
        assert active[0].name == "Key 2"

        all_keys = list_user_api_keys(db, user.id, active_only=False)
        assert len(all_keys) == 2

    def test_revoke_api_key(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        key = self._make_api_key(user.id)
        create_api_key(db, key)
        revoke_api_key(db, key.id)

        fetched = get_api_key_by_hash(db, key.key_hash)
        assert fetched is not None
        assert fetched.is_active is False


@pytest.mark.unit
class TestDatabaseIntegrity:
    """Tests for database-level constraints and behaviors."""

    def test_wal_mode_enabled(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        # WAL mode is set per-connection by _connect(); verify via CRUD helper
        from dango.auth.database import _connect

        conn = _connect(db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        conn.close()
        assert mode is not None
        assert mode[0] == "wal"

    def test_foreign_keys_enabled(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _make_user()
        create_user(db, user)
        # Try inserting a session with a non-existent user_id
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sessions (id, user_id, token_hash, is_active, is_partial, "
                "created_at, expires_at, last_activity) "
                "VALUES ('s1', 'nonexistent', 'hash', 1, 0, '2026-01-01', '2026-01-02', '2026-01-01')"
            )
        conn.close()
