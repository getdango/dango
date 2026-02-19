"""tests/unit/test_auth_api_keys.py

Tests for API key lifecycle management in dango/auth/sessions.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from dango.auth import database as db
from dango.auth.models import Role, User
from dango.auth.security import hash_api_key
from dango.auth.sessions import (
    create_api_key,
    list_api_keys,
    revoke_api_key,
    validate_api_key,
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
class TestCreateAPIKey:
    """Tests for create_api_key()."""

    def test_returns_raw_key_and_model(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_key, api_key = create_api_key(db_path, user.id, "My Key")
        assert isinstance(raw_key, str)
        assert api_key.name == "My Key"
        assert api_key.user_id == user.id

    def test_dango_ak_prefix(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_key, _ = create_api_key(db_path, user.id, "Key")
        assert raw_key.startswith("dango_ak_")

    def test_hash_stored_not_raw(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_key, api_key = create_api_key(db_path, user.id, "Key")
        assert api_key.key_hash == hash_api_key(raw_key)
        assert api_key.key_hash != raw_key

    def test_prefix_set(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_key, api_key = create_api_key(db_path, user.id, "Key")
        assert api_key.key_prefix == raw_key[:12]

    def test_with_expiry(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        exp = datetime.now(timezone.utc) + timedelta(days=90)
        _, api_key = create_api_key(db_path, user.id, "Key", expires_at=exp)
        assert api_key.expires_at is not None
        assert abs((api_key.expires_at - exp).total_seconds()) < 2

    def test_without_expiry(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        _, api_key = create_api_key(db_path, user.id, "Key")
        assert api_key.expires_at is None


@pytest.mark.unit
class TestValidateAPIKey:
    """Tests for validate_api_key()."""

    def test_happy_path_returns_user(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_key, _ = create_api_key(db_path, user.id, "Key")
        result = validate_api_key(db_path, raw_key)
        assert result is not None
        assert result.id == user.id

    def test_updates_last_used(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_key, api_key = create_api_key(db_path, user.id, "Key")
        assert api_key.last_used_at is None
        validate_api_key(db_path, raw_key)
        fetched = db.get_api_key_by_hash(db_path, api_key.key_hash)
        assert fetched is not None
        assert fetched.last_used_at is not None

    def test_invalid_key_returns_none(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        assert validate_api_key(db_path, "dango_ak_bogus") is None

    def test_revoked_key_returns_none(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_key, api_key = create_api_key(db_path, user.id, "Key")
        revoke_api_key(db_path, api_key.id)
        assert validate_api_key(db_path, raw_key) is None

    def test_expired_key_returns_none(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        raw_key, _ = create_api_key(db_path, user.id, "Key", expires_at=past)
        assert validate_api_key(db_path, raw_key) is None

    def test_no_expiry_always_valid(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_key, _ = create_api_key(db_path, user.id, "Key")
        assert validate_api_key(db_path, raw_key) is not None

    def test_deactivated_user_returns_none(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_key, _ = create_api_key(db_path, user.id, "Key")
        db.deactivate_user(db_path, user.id)
        assert validate_api_key(db_path, raw_key) is None


@pytest.mark.unit
class TestRevokeAPIKey:
    """Tests for revoke_api_key()."""

    def test_revokes_target(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        raw_key, api_key = create_api_key(db_path, user.id, "Key")
        revoke_api_key(db_path, api_key.id)
        assert validate_api_key(db_path, raw_key) is None

    def test_others_unaffected(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        _, key1 = create_api_key(db_path, user.id, "Key 1")
        raw2, _ = create_api_key(db_path, user.id, "Key 2")
        revoke_api_key(db_path, key1.id)
        assert validate_api_key(db_path, raw2) is not None


@pytest.mark.unit
class TestListAPIKeys:
    """Tests for list_api_keys()."""

    def test_lists_active(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        create_api_key(db_path, user.id, "Key 1")
        create_api_key(db_path, user.id, "Key 2")
        keys = list_api_keys(db_path, user.id)
        assert len(keys) == 2

    def test_excludes_revoked(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        _, key1 = create_api_key(db_path, user.id, "Key 1")
        create_api_key(db_path, user.id, "Key 2")
        revoke_api_key(db_path, key1.id)
        keys = list_api_keys(db_path, user.id)
        assert len(keys) == 1
        assert keys[0].name == "Key 2"

    def test_empty_for_no_keys(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _setup_user(db_path)
        assert list_api_keys(db_path, user.id) == []
