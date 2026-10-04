"""tests/unit/test_cli_auth_metabase_repair_admin.py

CLI tests for 'dango auth metabase-repair' on the Metabase admin account, which must
re-sync the SSO copy from the working admin credential and never rotate the password.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner, Result

from dango.auth.database import create_user, get_user_by_email
from dango.auth.metabase_sync import decrypt_metabase_password, encrypt_metabase_password
from dango.auth.models import Role, User
from dango.cli.main import cli
from dango.migrations.runner import MigrationRunner

ADMIN = "admin@test.com"
CREDS = (ADMIN, "working-admin-pw")


def _setup_project(tmp_path: Path) -> Path:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "project.yml").write_text("project:\n  name: test\n")
    (dango_dir / "metabase.yml").write_text(
        yaml.safe_dump(
            {
                "metabase_url": "http://localhost:3000",
                "admin": {"email": ADMIN, "password": "yml-pw"},
            }
        )
    )
    migrations = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    MigrationRunner(
        db_path=dango_dir / "auth.db", db_name="auth", migrations_dir=migrations
    ).apply_pending()
    return tmp_path


def _add_linked_user(root: Path, *, email: str, mb_user_id: int, password: str = "old-pw") -> None:
    create_user(
        root / ".dango" / "auth.db",
        User(
            email=email,
            password_hash="$2b$12$fake",
            role=Role.VIEWER,
            metabase_user_id=mb_user_id,
            metabase_password_enc=encrypt_metabase_password(password, root),
        ),
    )


def _resp(status: int) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    return r


def _invoke(project_root: Path, args: list[str]) -> Result:
    with (
        patch("dango.cli.utils.find_project_root", return_value=project_root),
        patch("dango.security.metabase_config.load_metabase_admin_credentials", return_value=CREDS),
    ):
        return CliRunner().invoke(cli, ["auth", "metabase-repair", *args])


@pytest.mark.unit
def test_repair_admin_resyncs_sso_copy_without_rotation(tmp_path: Path) -> None:
    root = _setup_project(tmp_path)
    _add_linked_user(root, email=ADMIN, mb_user_id=1, password="stale-pw")

    with (
        patch("dango.auth.metabase_sync.update_metabase_user_password") as update_pw,
        patch("requests.post", return_value=_resp(200)),
    ):
        result = _invoke(root, [ADMIN])  # no --yes: no prompt on the admin path

    assert result.exit_code == 0, result.output
    assert "Re-synced and verified" in result.output
    update_pw.assert_not_called()
    user = get_user_by_email(root / ".dango" / "auth.db", ADMIN)
    assert user is not None and user.metabase_password_enc is not None
    assert decrypt_metabase_password(user.metabase_password_enc, root) == "working-admin-pw"


@pytest.mark.unit
def test_repair_admin_verification_failure_aborts(tmp_path: Path) -> None:
    root = _setup_project(tmp_path)
    _add_linked_user(root, email=ADMIN, mb_user_id=1)

    with (
        patch("dango.auth.metabase_sync.update_metabase_user_password") as update_pw,
        patch("requests.post", return_value=_resp(401)),
    ):
        result = _invoke(root, [ADMIN])

    assert result.exit_code != 0
    assert "Re-synced the SSO credential" in result.output
    update_pw.assert_not_called()


@pytest.mark.unit
def test_repair_non_admin_path_unchanged(tmp_path: Path) -> None:
    root = _setup_project(tmp_path)
    _add_linked_user(root, email="member@test.com", mb_user_id=2, password="old-pw")

    with (
        patch("dango.auth.metabase_sync._get_admin_session", return_value="sess"),
        patch("dango.auth.metabase_sync.update_metabase_user_password", return_value=True) as upd,
        patch("dango.auth.metabase_sync.generate_metabase_password", return_value="fresh-pw"),
        patch("requests.post", return_value=_resp(200)),
    ):
        result = _invoke(root, ["member@test.com", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Repaired and verified" in result.output
    upd.assert_called_once()
    user = get_user_by_email(root / ".dango" / "auth.db", "member@test.com")
    assert user is not None and user.metabase_password_enc is not None
    assert decrypt_metabase_password(user.metabase_password_enc, root) == "fresh-pw"
