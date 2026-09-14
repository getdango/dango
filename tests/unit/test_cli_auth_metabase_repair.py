"""tests/unit/test_cli_auth_metabase_repair.py

CLI tests for the Metabase bridge password diagnose/repair commands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from dango.auth.database import create_user, get_user_by_email
from dango.auth.metabase_sync import encrypt_metabase_password
from dango.auth.models import Role, User
from dango.cli.main import cli
from dango.migrations.runner import MigrationRunner

MB_URL = "http://localhost:3000"

# `dango/cli/commands/auth.py` uses lazy imports inside each command body (both for
# `dango.*` modules and for `requests`), matching the module's established convention.
# That means there is no persistent `dango.cli.commands.auth.requests` (etc.) attribute
# to patch. Per the codebase's cross-level-import testing pattern, the correct patch
# target is the *source* module (`dango.auth.metabase_sync`) for dango symbols, and the
# real `requests` module for HTTP calls — patching `requests.post` works regardless of
# whether the caller imported `requests` at module or function scope, since the lazy
# `import requests` just rebinds the name to the same (patched) module object.


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


def _mb_yml(project_root: Path, db_id: int = 1) -> None:
    d = project_root / ".dango"
    (d / "metabase.yml").write_text(
        yaml.safe_dump(
            {
                "metabase_url": MB_URL,
                "admin": {"email": "admin@test.com", "password": "testpassword123"},
                "database": {"id": db_id, "name": "Test Analytics"},
            }
        )
    )


def _make_user(**overrides: Any) -> User:
    defaults: dict[str, Any] = {
        "email": "test@example.com",
        "password_hash": "$2b$12$fakehashfakehashfakehashfakehashfakehashfakehashfakeh",
        "role": Role.VIEWER,
    }
    defaults.update(overrides)
    return User(**defaults)


def _add_user(tmp_path: Path, **overrides: Any) -> User:
    db_path = tmp_path / ".dango" / "auth.db"
    user = _make_user(**overrides)
    create_user(db_path, user)
    return user


def _add_linked_user(
    project_root: Path, *, email: str, mb_user_id: int, password: str = "old-password"
) -> User:
    """Add a Dango user already linked to a Metabase account with a stored password."""
    encrypted = encrypt_metabase_password(password, project_root)
    return _add_user(
        project_root,
        email=email,
        metabase_user_id=mb_user_id,
        metabase_password_enc=encrypted,
    )


def _resp(status: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    return r


@pytest.mark.unit
class TestMetabaseStatus:
    """Tests for 'dango auth metabase-status'."""

    def test_metabase_status_all_ok(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _mb_yml(project_root)
        _add_linked_user(project_root, email="a@test.com", mb_user_id=1)
        _add_linked_user(project_root, email="b@test.com", mb_user_id=2)

        runner = CliRunner()
        with (
            patch("dango.cli.utils.find_project_root", return_value=project_root),
            patch("requests.post", return_value=_resp(200)),
        ):
            result = runner.invoke(cli, ["auth", "metabase-status"])

        assert result.exit_code == 0, result.output
        assert "OK" in result.output
        assert "DESYNCED" not in result.output
        assert "metabase-repair" not in result.output

    def test_metabase_status_detects_desync(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _mb_yml(project_root)
        _add_linked_user(project_root, email="good@test.com", mb_user_id=1)
        _add_linked_user(project_root, email="bad@test.com", mb_user_id=2)

        def _post(url: str, json: dict[str, Any], timeout: int) -> MagicMock:
            if json["username"] == "bad@test.com":
                return _resp(401)
            return _resp(200)

        runner = CliRunner()
        with (
            patch("dango.cli.utils.find_project_root", return_value=project_root),
            patch("requests.post", side_effect=_post),
        ):
            result = runner.invoke(cli, ["auth", "metabase-status"])

        assert result.exit_code == 0, result.output
        assert "DESYNCED" in result.output
        assert "OK" in result.output
        assert "metabase-repair" in result.output

    def test_metabase_status_single_email(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _mb_yml(project_root)
        _add_linked_user(project_root, email="a@test.com", mb_user_id=1)
        _add_linked_user(project_root, email="b@test.com", mb_user_id=2)

        runner = CliRunner()
        with (
            patch("dango.cli.utils.find_project_root", return_value=project_root),
            patch("requests.post", return_value=_resp(200)) as mock_post,
        ):
            result = runner.invoke(cli, ["auth", "metabase-status", "a@test.com"])

        assert result.exit_code == 0, result.output
        assert mock_post.call_count == 1
        assert "a@test.com" in result.output
        assert "b@test.com" not in result.output

    def test_metabase_status_user_not_linked(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _mb_yml(project_root)
        _add_user(project_root, email="unlinked@test.com", metabase_user_id=None)

        runner = CliRunner()
        with (
            patch("dango.cli.utils.find_project_root", return_value=project_root),
            patch("requests.post") as mock_post,
        ):
            result = runner.invoke(cli, ["auth", "metabase-status"])

        assert result.exit_code == 0, result.output
        assert "No Metabase-linked users to check." in result.output
        assert mock_post.call_count == 0

    def test_metabase_status_unknown_email(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _mb_yml(project_root)

        runner = CliRunner()
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = runner.invoke(cli, ["auth", "metabase-status", "nonexistent@example.com"])

        assert result.exit_code != 0
        assert "nonexistent@example.com" in result.output
        assert "not found" in result.output.lower()


@pytest.mark.unit
class TestMetabaseRepair:
    """Tests for 'dango auth metabase-repair'."""

    def test_metabase_repair_success(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _mb_yml(project_root)
        user = _add_linked_user(project_root, email="user@test.com", mb_user_id=42)
        old_encrypted = user.metabase_password_enc

        runner = CliRunner()
        with (
            patch("dango.cli.utils.find_project_root", return_value=project_root),
            patch(
                "dango.auth.metabase_sync._get_admin_session", return_value="admin-session-token"
            ),
            patch(
                "dango.auth.metabase_sync.update_metabase_user_password", return_value=True
            ) as mock_update_pw,
            patch("requests.post", return_value=_resp(200)),
        ):
            result = runner.invoke(cli, ["auth", "metabase-repair", "user@test.com", "--yes"])

        assert result.exit_code == 0, result.output
        assert "Repaired and verified" in result.output
        mock_update_pw.assert_called_once()

        db_path = project_root / ".dango" / "auth.db"
        updated = get_user_by_email(db_path, "user@test.com")
        assert updated is not None
        assert updated.metabase_password_enc != old_encrypted

        audit_log = project_root / ".dango" / "logs" / "audit.jsonl"
        assert audit_log.exists()
        contents = audit_log.read_text()
        assert "password_reset" in contents
        assert "metabase_bridge" in contents

    def test_metabase_repair_requires_confirmation(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _mb_yml(project_root)
        _add_linked_user(project_root, email="user@test.com", mb_user_id=42)

        runner = CliRunner()
        with (
            patch("dango.cli.utils.find_project_root", return_value=project_root),
            patch("click.confirm", return_value=False),
            patch("dango.auth.metabase_sync.update_metabase_user_password") as mock_update_pw,
        ):
            result = runner.invoke(cli, ["auth", "metabase-repair", "user@test.com"])

        assert result.exit_code == 0, result.output
        assert "Aborted" in result.output
        mock_update_pw.assert_not_called()

    def test_metabase_repair_unlinked_user_errors(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _mb_yml(project_root)
        _add_user(project_root, email="unlinked@test.com", metabase_user_id=None)

        runner = CliRunner()
        with (
            patch("dango.cli.utils.find_project_root", return_value=project_root),
            patch("dango.auth.metabase_sync.update_metabase_user_password") as mock_update_pw,
        ):
            result = runner.invoke(cli, ["auth", "metabase-repair", "unlinked@test.com", "--yes"])

        assert result.exit_code != 0
        assert "nothing to repair" in " ".join(result.output.lower().split())
        mock_update_pw.assert_not_called()

    def test_metabase_repair_metabase_update_fails(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _mb_yml(project_root)
        user = _add_linked_user(project_root, email="user@test.com", mb_user_id=42)
        old_encrypted = user.metabase_password_enc

        runner = CliRunner()
        with (
            patch("dango.cli.utils.find_project_root", return_value=project_root),
            patch(
                "dango.auth.metabase_sync._get_admin_session", return_value="admin-session-token"
            ),
            patch(
                "dango.auth.metabase_sync.update_metabase_user_password", return_value=False
            ) as mock_update_pw,
        ):
            result = runner.invoke(cli, ["auth", "metabase-repair", "user@test.com", "--yes"])

        assert result.exit_code != 0
        assert "Failed to update password" in result.output
        mock_update_pw.assert_called_once()

        db_path = project_root / ".dango" / "auth.db"
        updated = get_user_by_email(db_path, "user@test.com")
        assert updated is not None
        assert updated.metabase_password_enc == old_encrypted

    def test_metabase_repair_verification_fails(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        _mb_yml(project_root)
        _add_linked_user(project_root, email="user@test.com", mb_user_id=42)

        runner = CliRunner()
        with (
            patch("dango.cli.utils.find_project_root", return_value=project_root),
            patch(
                "dango.auth.metabase_sync._get_admin_session", return_value="admin-session-token"
            ),
            patch("dango.auth.metabase_sync.update_metabase_user_password", return_value=True),
            patch("requests.post", return_value=_resp(401)),
        ):
            result = runner.invoke(cli, ["auth", "metabase-repair", "user@test.com", "--yes"])

        assert result.exit_code != 0
        assert "verification login still failed" in " ".join(result.output.split())

        # The local DB write already happened even though verification failed.
        db_path = project_root / ".dango" / "auth.db"
        updated = get_user_by_email(db_path, "user@test.com")
        assert updated is not None
        assert updated.metabase_password_enc is not None
