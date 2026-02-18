"""tests/unit/test_auth_models.py

Tests for auth Pydantic models in dango/auth/models.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dango.auth.models import APIKey, Role, Session, User, UserCreate, UserResponse, UserUpdate


@pytest.mark.unit
class TestRole:
    """Tests for the Role enum."""

    def test_values(self) -> None:
        assert Role.ADMIN.value == "admin"
        assert Role.EDITOR.value == "editor"
        assert Role.VIEWER.value == "viewer"

    def test_is_str_subclass(self) -> None:
        assert isinstance(Role.ADMIN, str)
        assert Role.ADMIN == "admin"

    def test_from_string(self) -> None:
        assert Role("admin") is Role.ADMIN
        assert Role("editor") is Role.EDITOR
        assert Role("viewer") is Role.VIEWER

    def test_invalid_role_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid"):
            Role("superadmin")


@pytest.mark.unit
class TestUser:
    """Tests for the User model."""

    def test_defaults(self) -> None:
        user = User(email="test@example.com")
        assert user.id  # UUID auto-generated
        assert user.email == "test@example.com"
        assert user.password_hash is None
        assert user.role is Role.VIEWER
        assert user.is_active is True
        assert user.totp_secret is None
        assert user.totp_enabled is False
        assert user.recovery_codes is None
        assert user.oauth_provider is None
        assert user.oauth_id is None
        assert user.failed_login_attempts == 0
        assert user.locked_until is None
        assert isinstance(user.created_at, datetime)
        assert isinstance(user.updated_at, datetime)

    def test_uuid_auto_generated(self) -> None:
        user1 = User(email="a@example.com")
        user2 = User(email="b@example.com")
        assert user1.id != user2.id

    def test_email_normalization(self) -> None:
        user = User(email="  TEST@Example.COM  ")
        assert user.email == "test@example.com"

    def test_all_fields_explicit(self) -> None:
        now = datetime.now(timezone.utc)
        user = User(
            id="custom-id",
            email="admin@test.com",
            password_hash="hashed",
            role=Role.ADMIN,
            is_active=False,
            totp_secret="JBSWY3DPEHPK3PXP",
            totp_enabled=True,
            recovery_codes="code1,code2",
            oauth_provider="google",
            oauth_id="google-123",
            failed_login_attempts=3,
            locked_until=now,
            created_at=now,
            updated_at=now,
        )
        assert user.id == "custom-id"
        assert user.password_hash == "hashed"
        assert user.role is Role.ADMIN
        assert user.is_active is False
        assert user.totp_enabled is True
        assert user.failed_login_attempts == 3


@pytest.mark.unit
class TestUserCreate:
    """Tests for the UserCreate model."""

    def test_required_email_only(self) -> None:
        uc = UserCreate(email="user@example.com")
        assert uc.email == "user@example.com"
        assert uc.password is None
        assert uc.role is Role.VIEWER
        assert uc.oauth_provider is None
        assert uc.oauth_id is None

    def test_with_password(self) -> None:
        uc = UserCreate(email="user@example.com", password="secret123")
        assert uc.password == "secret123"

    def test_with_role(self) -> None:
        uc = UserCreate(email="admin@example.com", role=Role.ADMIN)
        assert uc.role is Role.ADMIN

    def test_email_normalization(self) -> None:
        uc = UserCreate(email="  Admin@Test.COM  ")
        assert uc.email == "admin@test.com"

    def test_oauth_fields(self) -> None:
        uc = UserCreate(
            email="user@example.com",
            oauth_provider="github",
            oauth_id="gh-456",
        )
        assert uc.oauth_provider == "github"
        assert uc.oauth_id == "gh-456"


@pytest.mark.unit
class TestUserUpdate:
    """Tests for the UserUpdate model."""

    def test_all_fields_optional(self) -> None:
        uu = UserUpdate()
        dump = uu.model_dump(exclude_unset=True)
        assert dump == {}

    def test_partial_update_email(self) -> None:
        uu = UserUpdate(email="new@example.com")
        dump = uu.model_dump(exclude_unset=True)
        assert dump == {"email": "new@example.com"}

    def test_partial_update_role(self) -> None:
        uu = UserUpdate(role=Role.EDITOR)
        dump = uu.model_dump(exclude_unset=True)
        assert dump == {"role": Role.EDITOR}

    def test_email_normalization(self) -> None:
        uu = UserUpdate(email="  UPPER@Test.COM  ")
        assert uu.email == "upper@test.com"

    def test_email_none_passes_through(self) -> None:
        uu = UserUpdate(email=None)
        assert uu.email is None

    def test_multiple_fields(self) -> None:
        uu = UserUpdate(email="new@test.com", role=Role.ADMIN, is_active=False)
        dump = uu.model_dump(exclude_unset=True)
        assert dump["email"] == "new@test.com"
        assert dump["role"] == Role.ADMIN
        assert dump["is_active"] is False

    def test_exclude_unset_preserves_falsy_values(self) -> None:
        uu = UserUpdate(failed_login_attempts=0, totp_enabled=False)
        dump = uu.model_dump(exclude_unset=True)
        assert dump["failed_login_attempts"] == 0
        assert dump["totp_enabled"] is False

    def test_exclude_unset_preserves_explicit_none(self) -> None:
        uu = UserUpdate(locked_until=None)
        dump = uu.model_dump(exclude_unset=True)
        assert "locked_until" in dump
        assert dump["locked_until"] is None


@pytest.mark.unit
class TestUserResponse:
    """Tests for the UserResponse model."""

    def test_from_user_model(self) -> None:
        user = User(email="test@example.com", password_hash="secret", totp_secret="totp123")
        response = UserResponse.model_validate(user)
        assert response.email == "test@example.com"
        assert response.id == user.id
        assert response.role is Role.VIEWER
        assert response.is_active is True

    def test_sensitive_fields_absent(self) -> None:
        fields = set(UserResponse.model_fields.keys())
        assert "password_hash" not in fields
        assert "totp_secret" not in fields
        assert "recovery_codes" not in fields
        assert "oauth_id" not in fields

    def test_safe_fields_present(self) -> None:
        fields = set(UserResponse.model_fields.keys())
        assert "id" in fields
        assert "email" in fields
        assert "role" in fields
        assert "is_active" in fields
        assert "totp_enabled" in fields
        assert "oauth_provider" in fields
        assert "created_at" in fields
        assert "updated_at" in fields


@pytest.mark.unit
class TestSession:
    """Tests for the Session model."""

    def test_construction(self) -> None:
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        session = Session(user_id="user-1", token_hash="hash123", expires_at=expires)
        assert session.id  # UUID auto-generated
        assert session.user_id == "user-1"
        assert session.token_hash == "hash123"
        assert session.is_active is True
        assert session.is_partial is False
        assert isinstance(session.created_at, datetime)
        assert session.expires_at == expires
        assert isinstance(session.last_activity, datetime)

    def test_partial_session(self) -> None:
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        session = Session(
            user_id="user-1",
            token_hash="hash456",
            expires_at=expires,
            is_partial=True,
        )
        assert session.is_partial is True


@pytest.mark.unit
class TestAPIKey:
    """Tests for the APIKey model."""

    def test_construction(self) -> None:
        key = APIKey(user_id="user-1", name="My Key", key_hash="keyhash123")
        assert key.id  # UUID auto-generated
        assert key.user_id == "user-1"
        assert key.name == "My Key"
        assert key.key_hash == "keyhash123"
        assert key.is_active is True
        assert isinstance(key.created_at, datetime)
        assert key.last_used_at is None
        assert key.expires_at is None

    def test_with_expiry(self) -> None:
        expires = datetime.now(timezone.utc) + timedelta(days=90)
        key = APIKey(
            user_id="user-1",
            name="Expiring Key",
            key_hash="keyhash456",
            expires_at=expires,
        )
        assert key.expires_at == expires

    def test_required_fields(self) -> None:
        with pytest.raises(ValueError):
            APIKey.model_validate({})  # missing user_id, name, key_hash
