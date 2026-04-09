"""tests/unit/test_cli_auth.py

CLI command tests for dango/cli/commands/auth.py using CliRunner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.auth.database import create_user
from dango.auth.models import Role, User
from dango.cli.main import cli
from dango.migrations.runner import MigrationRunner


def _setup_project(tmp_path: Path) -> Path:
    """Create a minimal dango project with auth.db."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "project.yml").write_text("project:\n  name: test\n")

    db_path = dango_dir / "auth.db"
    migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    runner = MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=migrations_dir)
    runner.apply_pending()
    return tmp_path


def _make_user(**overrides: Any) -> User:
    """Build a User model with sensible defaults."""
    defaults: dict[str, Any] = {
        "email": "test@example.com",
        "password_hash": "$2b$12$fakehashfakehashfakehashfakehashfakehashfakehashfakeh",
        "role": Role.VIEWER,
    }
    defaults.update(overrides)
    return User(**defaults)


def _add_user(tmp_path: Path, **overrides: Any) -> User:
    """Insert a user into the project's auth.db."""
    db_path = tmp_path / ".dango" / "auth.db"
    user = _make_user(**overrides)
    create_user(db_path, user)
    return user


@pytest.mark.unit
class TestAuthHelp:
    """Auth group --help shows all subcommands."""

    def test_help_lists_subcommands(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["auth", "--help"])
        assert result.exit_code == 0
        for cmd in [
            "enable",
            "disable",
            "add-user",
            "list-users",
            "reset-password",
            "deactivate-user",
            "reactivate-user",
            "delete-user",
            "status",
            "unlock",
            "audit",
            "recover",
        ]:
            assert cmd in result.output, f"Subcommand '{cmd}' not in --help"


@pytest.mark.unit
class TestAuthEnable:
    """Tests for 'dango auth enable'."""

    def test_enable_creates_config(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "enable"], input="admin@test.com\n")
        assert result.exit_code == 0
        assert "enabled" in result.output.lower()

    def test_enable_already_enabled(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        config_path = project_root / ".dango" / "auth.yml"
        config_path.write_text("enabled: true\n")
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "enable"])
        assert result.exit_code == 0
        assert "already enabled" in result.output.lower()

    def test_enable_creates_admin(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "enable"], input="admin@test.com\n")
        assert result.exit_code == 0
        assert "admin@test.com" in result.output

    def test_enable_skips_admin_if_exists(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="admin@test.com", role=Role.ADMIN)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "enable"], input="admin@test.com\n")
        assert result.exit_code == 0
        assert "already exists" in result.output.lower()


@pytest.mark.unit
class TestAuthDisable:
    """Tests for 'dango auth disable'."""

    def test_disable_with_confirmation(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        (project_root / ".dango" / "auth.yml").write_text("enabled: true\n")
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "disable"], input="y\n")
        assert result.exit_code == 0
        assert "disabled" in result.output.lower()

    def test_disable_already_disabled(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "disable"])
        assert result.exit_code == 0
        assert "already disabled" in result.output.lower()

    def test_disable_aborted(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        (project_root / ".dango" / "auth.yml").write_text("enabled: true\n")
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "disable"], input="n\n")
        assert result.exit_code == 0
        # Auth should still be enabled
        from dango.auth.admin import is_auth_enabled

        assert is_auth_enabled(project_root) is True


@pytest.mark.unit
class TestAuthAddUser:
    """Tests for 'dango auth add-user'."""

    def test_add_user_default_role(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "add-user", "new@test.com"])
        assert result.exit_code == 0
        assert "new@test.com" in result.output

    def test_add_user_with_role(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "add-user", "ed@test.com", "--role", "editor"])
        assert result.exit_code == 0
        assert "ed@test.com" in result.output

    def test_add_duplicate_user(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="dup@test.com")
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "add-user", "dup@test.com"])
        assert result.exit_code != 0
        assert "already exists" in result.output.lower()


@pytest.mark.unit
class TestAuthListUsers:
    """Tests for 'dango auth list-users'."""

    def test_empty_list(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "list-users"])
        assert result.exit_code == 0
        assert "no users" in result.output.lower()

    def test_list_with_users(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="alice@test.com", role=Role.ADMIN)
        _add_user(project_root, email="bob@test.com", role=Role.VIEWER)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "list-users"])
        assert result.exit_code == 0
        assert "alice@test.com" in result.output
        assert "bob@test.com" in result.output


@pytest.mark.unit
class TestAuthResetPassword:
    """Tests for 'dango auth reset-password'."""

    def test_reset_existing_user(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="user@test.com")
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "reset-password", "user@test.com"])
        assert result.exit_code == 0
        assert "user@test.com" in result.output

    def test_reset_changes_hash_and_invalidates_sessions(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        db_path = project_root / ".dango" / "auth.db"
        user = _add_user(project_root, email="user@test.com")
        old_hash = user.password_hash

        # Create a session for the user
        from datetime import datetime, timedelta, timezone

        from dango.auth.database import create_session, get_user_by_email, list_user_sessions
        from dango.auth.models import Session

        session = Session(
            user_id=user.id,
            token_hash="fakehash123",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        create_session(db_path, session)
        assert len(list_user_sessions(db_path, user.id)) == 1

        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "reset-password", "user@test.com"])
        assert result.exit_code == 0

        updated = get_user_by_email(db_path, "user@test.com")
        assert updated is not None
        assert updated.password_hash != old_hash
        assert updated.must_change_password is True
        assert len(list_user_sessions(db_path, user.id, active_only=True)) == 0

    def test_reset_nonexistent_user(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "reset-password", "gone@test.com"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()


@pytest.mark.unit
class TestAuthDeactivateUser:
    """Tests for 'dango auth deactivate-user'."""

    def test_deactivate_user(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="user@test.com", role=Role.VIEWER)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "deactivate-user", "user@test.com"])
        assert result.exit_code == 0
        assert "deactivated" in result.output.lower()

    def test_deactivate_admin_when_another_exists(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="admin1@test.com", role=Role.ADMIN)
        _add_user(project_root, email="admin2@test.com", role=Role.ADMIN)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "deactivate-user", "admin1@test.com"])
        assert result.exit_code == 0
        assert "deactivated" in result.output.lower()

    def test_deactivate_last_admin_blocked(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="admin@test.com", role=Role.ADMIN)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "deactivate-user", "admin@test.com"])
        assert result.exit_code != 0
        assert "only active admin" in result.output.lower()

    def test_deactivate_nonexistent(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "deactivate-user", "nope@test.com"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()


@pytest.mark.unit
class TestAuthReactivateUser:
    """Tests for 'dango auth reactivate-user'."""

    def test_reactivate_inactive_user(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        user = _add_user(project_root, email="user@test.com")
        from dango.auth.database import deactivate_user

        deactivate_user(project_root / ".dango" / "auth.db", user.id)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "reactivate-user", "user@test.com"])
        assert result.exit_code == 0
        assert "reactivated" in result.output.lower()

    def test_reactivate_already_active(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="user@test.com")
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "reactivate-user", "user@test.com"])
        assert result.exit_code == 0
        assert "already active" in result.output.lower()


@pytest.mark.unit
class TestAuthDeleteUser:
    """Tests for 'dango auth delete-user'."""

    def test_delete_with_confirmation(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="user@test.com")
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "delete-user", "user@test.com"], input="y\n")
        assert result.exit_code == 0
        assert "deleted" in result.output.lower()

    def test_delete_aborted(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="user@test.com")
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "delete-user", "user@test.com"], input="n\n")
        assert result.exit_code == 0
        # User should still exist
        from dango.auth.database import get_user_by_email

        assert get_user_by_email(project_root / ".dango" / "auth.db", "user@test.com") is not None

    def test_delete_last_admin_blocked(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="admin@test.com", role=Role.ADMIN)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "delete-user", "admin@test.com"], input="y\n")
        assert result.exit_code != 0
        assert "only active admin" in result.output.lower()

    def test_delete_nonexistent(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "delete-user", "nope@test.com"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()


@pytest.mark.unit
class TestAuthStatus:
    """Tests for 'dango auth status'."""

    def test_status_no_db(self, tmp_path: Path) -> None:
        """Status works when auth.db doesn't exist yet."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "project.yml").write_text("project:\n  name: test\n")
        # No auth.db created
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=tmp_path):
            result = runner.invoke(cli, ["auth", "status"])
        assert result.exit_code == 0
        assert "not initialized" in result.output.lower()

    def test_status_disabled(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "status"])
        assert result.exit_code == 0
        assert "disabled" in result.output.lower()

    def test_status_enabled_with_users(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        (project_root / ".dango" / "auth.yml").write_text("enabled: true\n")
        _add_user(project_root, email="admin@test.com", role=Role.ADMIN)
        _add_user(project_root, email="viewer@test.com", role=Role.VIEWER)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "status"])
        assert result.exit_code == 0
        assert "enabled" in result.output.lower()
        assert "2 active" in result.output.lower()


@pytest.mark.unit
class TestAuthUnlock:
    """Tests for 'dango auth unlock'."""

    def test_unlock_locked_user(self, tmp_path: Path) -> None:
        from datetime import datetime, timezone

        project_root = _setup_project(tmp_path)
        user = _add_user(project_root, email="locked@test.com")
        from dango.auth.database import update_user
        from dango.auth.models import UserUpdate

        update_user(
            project_root / ".dango" / "auth.db",
            user.id,
            UserUpdate(failed_login_attempts=5, locked_until=datetime.now(timezone.utc)),
        )
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "unlock", "locked@test.com"])
        assert result.exit_code == 0
        assert "unlocked" in result.output.lower()

    def test_unlock_not_locked(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="user@test.com")
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "unlock", "user@test.com"])
        assert result.exit_code == 0
        assert "not locked" in result.output.lower()


@pytest.mark.unit
class TestAuthRecover:
    """Tests for 'dango auth recover'."""

    def test_recover_creates_admin(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "recover"], input="recovery@test.com\n")
        assert result.exit_code == 0
        assert "recovery@test.com" in result.output

    def test_recover_duplicate_email(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _add_user(project_root, email="existing@test.com")
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "recover"], input="existing@test.com\n")
        assert result.exit_code != 0
        assert "already exists" in result.output.lower()


@pytest.mark.unit
class TestAuthAudit:
    """Tests for 'dango auth audit'."""

    def test_audit_empty_log(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "audit"])
        assert result.exit_code == 0
        assert "no audit events" in result.output.lower()

    def test_audit_shows_events(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        from dango.auth.audit import AuditEvent, log_auth_event

        log_dir = project_root / ".dango" / "logs"
        log_auth_event(AuditEvent.USER_CREATED, email="test@test.com", log_dir=log_dir)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "audit"])
        assert result.exit_code == 0
        assert "test@test.com" in result.output
        assert "user_created" in result.output

    def test_audit_invalid_event_type(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "audit", "--type", "bogus"])
        assert result.exit_code != 0
