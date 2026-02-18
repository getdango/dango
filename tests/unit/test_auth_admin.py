"""tests/unit/test_auth_admin.py

Tests for admin business logic in dango/auth/admin.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dango.auth.admin import (
    ensure_admin,
    format_credentials_panel,
    get_auth_config_path,
    get_auth_db_path,
    is_auth_enabled,
    set_auth_enabled,
)
from dango.auth.models import Role, User
from dango.migrations.runner import MigrationRunner


def _make_db(tmp_path: Path) -> Path:
    """Create a fresh auth database by running migrations."""
    db_path = tmp_path / ".dango" / "auth.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
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


@pytest.mark.unit
class TestGetAuthDbPath:
    """Test get_auth_db_path helper."""

    def test_returns_expected_path(self, tmp_path: Path) -> None:
        result = get_auth_db_path(tmp_path)
        assert result == tmp_path / ".dango" / "auth.db"

    def test_path_is_absolute(self, tmp_path: Path) -> None:
        result = get_auth_db_path(tmp_path)
        assert result.is_absolute()


@pytest.mark.unit
class TestGetAuthConfigPath:
    """Test get_auth_config_path helper."""

    def test_returns_expected_path(self, tmp_path: Path) -> None:
        result = get_auth_config_path(tmp_path)
        assert result == tmp_path / ".dango" / "auth.yml"


@pytest.mark.unit
class TestIsAuthEnabled:
    """Test is_auth_enabled reading auth.yml."""

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        assert is_auth_enabled(tmp_path) is False

    def test_empty_file_returns_false(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".dango" / "auth.yml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("")
        assert is_auth_enabled(tmp_path) is False

    def test_enabled_true(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".dango" / "auth.yml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("enabled: true\n")
        assert is_auth_enabled(tmp_path) is True

    def test_enabled_false(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".dango" / "auth.yml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("enabled: false\n")
        assert is_auth_enabled(tmp_path) is False


@pytest.mark.unit
class TestSetAuthEnabled:
    """Test set_auth_enabled writing auth.yml."""

    def test_creates_file(self, tmp_path: Path) -> None:
        set_auth_enabled(tmp_path, enabled=True)
        config_path = tmp_path / ".dango" / "auth.yml"
        assert config_path.exists()
        assert is_auth_enabled(tmp_path) is True

    def test_updates_existing(self, tmp_path: Path) -> None:
        set_auth_enabled(tmp_path, enabled=True)
        assert is_auth_enabled(tmp_path) is True
        set_auth_enabled(tmp_path, enabled=False)
        assert is_auth_enabled(tmp_path) is False

    def test_preserves_other_keys(self, tmp_path: Path) -> None:
        import yaml

        config_path = tmp_path / ".dango" / "auth.yml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("enabled: false\ncustom_key: hello\n")

        set_auth_enabled(tmp_path, enabled=True)

        with open(config_path) as f:
            data: dict[str, Any] = yaml.safe_load(f)
        assert data["enabled"] is True
        assert data["custom_key"] == "hello"


@pytest.mark.unit
class TestEnsureAdmin:
    """Test ensure_admin creating admin when needed."""

    def test_creates_admin_when_none_exist(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        result = ensure_admin(db_path, email="admin@test.com")

        assert result is not None
        user, password = result
        assert user.email == "admin@test.com"
        assert user.role == Role.ADMIN
        assert user.must_change_password is True
        assert len(password) == 12

    def test_skips_when_admin_exists(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        from dango.auth.database import create_user

        admin = _make_user(email="existing@test.com", role=Role.ADMIN)
        create_user(db_path, admin)

        result = ensure_admin(db_path, email="new@test.com")
        assert result is None

    def test_default_email(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        result = ensure_admin(db_path)
        assert result is not None
        user, _ = result
        assert user.email == "admin@localhost"

    def test_email_normalized(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        result = ensure_admin(db_path, email="  Admin@Test.COM  ")
        assert result is not None
        user, _ = result
        assert user.email == "admin@test.com"

    def test_ignores_inactive_admins(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        from dango.auth.database import create_user, deactivate_user

        admin = _make_user(email="old@test.com", role=Role.ADMIN)
        create_user(db_path, admin)
        deactivate_user(db_path, admin.id)

        result = ensure_admin(db_path, email="new@test.com")
        assert result is not None
        user, _ = result
        assert user.email == "new@test.com"


@pytest.mark.unit
class TestFormatCredentialsPanel:
    """Test format_credentials_panel output."""

    def test_contains_email(self) -> None:
        panel = format_credentials_panel("user@test.com", "temppass123")
        rendered = panel.renderable
        assert "user@test.com" in str(rendered)

    def test_contains_password(self) -> None:
        panel = format_credentials_panel("user@test.com", "temppass123")
        rendered = panel.renderable
        assert "temppass123" in str(rendered)

    def test_custom_title(self) -> None:
        panel = format_credentials_panel("u@t.com", "p", title="Custom Title")
        assert "Custom Title" in str(panel.title)

    def test_default_title(self) -> None:
        panel = format_credentials_panel("u@t.com", "p")
        assert "Admin account created" in str(panel.title)
