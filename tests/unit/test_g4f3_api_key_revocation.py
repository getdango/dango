"""tests/unit/test_g4f3_api_key_revocation.py

Credential-resetting actions also revoke the user's API keys, and creating a key re-checks the
current password.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.auth import database as db
from dango.auth.models import Role
from dango.auth.sessions import create_api_key, create_session, validate_api_key
from dango.cli.main import cli
from tests.unit.test_cli_auth import _add_user, _setup_project
from tests.unit.test_web_auth_2fa import _TEST_PASSWORD, _authed, _hdrs
from tests.unit.test_web_users import _auth_headers, _make_user, _setup_admin_client


def _key_for(db_path: Path, user_id: str) -> str:
    raw, _key = create_api_key(db_path, user_id, "ci")
    assert validate_api_key(db_path, raw) is not None
    return raw


@pytest.mark.unit
class TestKeysRevokedOnCredentialChange:
    def test_change_password_revokes_keys(self, tmp_path: Path) -> None:
        client, db_path, user = _authed(tmp_path)
        raw = _key_for(db_path, user.id)
        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": _TEST_PASSWORD, "new_password": "a-Brand-New-pass-77!"},
            headers=_hdrs(),
        )
        assert resp.status_code == 200
        assert validate_api_key(db_path, raw) is None
        assert db.list_user_api_keys(db_path, user.id, active_only=True) == []

    def test_admin_reset_password_revokes_keys(self, tmp_path: Path) -> None:
        client, db_path, _admin = _setup_admin_client(tmp_path)
        target = _make_user(db_path, email="user@example.com")
        raw = _key_for(db_path, target.id)
        resp = client.post(f"/api/admin/users/{target.id}/reset-password", headers=_auth_headers())
        assert resp.status_code == 200
        assert validate_api_key(db_path, raw) is None

    def test_admin_revoke_sessions_revokes_keys(self, tmp_path: Path) -> None:
        client, db_path, _admin = _setup_admin_client(tmp_path)
        target = _make_user(db_path, email="user@example.com")
        create_session(db_path, target.id)
        raw = _key_for(db_path, target.id)
        resp = client.post(f"/api/admin/users/{target.id}/revoke-sessions", headers=_auth_headers())
        assert resp.status_code == 200
        assert validate_api_key(db_path, raw) is None

    def test_deactivate_revokes_keys_for_good(self, tmp_path: Path) -> None:
        """Reactivation must not bring a key back."""
        client, db_path, _admin = _setup_admin_client(tmp_path)
        target = _make_user(db_path, email="user@example.com")
        raw = _key_for(db_path, target.id)
        assert (
            client.post(
                f"/api/admin/users/{target.id}/deactivate", headers=_auth_headers()
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/admin/users/{target.id}/reactivate", headers=_auth_headers()
            ).status_code
            == 200
        )
        assert validate_api_key(db_path, raw) is None

    def test_other_users_keys_are_untouched(self, tmp_path: Path) -> None:
        client, db_path, _admin = _setup_admin_client(tmp_path)
        target = _make_user(db_path, email="user@example.com")
        bystander = _make_user(db_path, email="other@example.com")
        raw_other = _key_for(db_path, bystander.id)
        client.post(f"/api/admin/users/{target.id}/reset-password", headers=_auth_headers())
        assert validate_api_key(db_path, raw_other) is not None

    def test_cli_reset_password_and_deactivate_revoke_keys(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        db_path = project_root / ".dango" / "auth.db"
        user = _add_user(project_root, email="user@test.com")
        _add_user(project_root, email="boss@test.com", role=Role.ADMIN)
        raw = _key_for(db_path, user.id)
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = CliRunner().invoke(cli, ["auth", "reset-password", "user@test.com"])
        assert result.exit_code == 0
        assert validate_api_key(db_path, raw) is None

        raw2 = _key_for(db_path, user.id)
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = CliRunner().invoke(cli, ["auth", "deactivate-user", "user@test.com"])
        assert result.exit_code == 0
        assert db.list_user_api_keys(db_path, user.id, active_only=True) == []
        assert raw2


@pytest.mark.unit
class TestApiKeyCreationNeedsPassword:
    def test_missing_password_is_rejected(self, tmp_path: Path) -> None:
        client, db_path, user = _authed(tmp_path)
        resp = client.post("/api/auth/api-keys", json={"name": "ci"}, headers=_hdrs())
        assert resp.status_code in (400, 422)
        assert db.list_user_api_keys(db_path, user.id, active_only=True) == []

    def test_wrong_password_is_rejected(self, tmp_path: Path) -> None:
        client, db_path, user = _authed(tmp_path)
        resp = client.post(
            "/api/auth/api-keys",
            json={"name": "ci", "current_password": "not-the-password"},
            headers=_hdrs(),
        )
        assert resp.status_code == 400
        assert db.list_user_api_keys(db_path, user.id, active_only=True) == []

    def test_correct_password_creates_a_key(self, tmp_path: Path) -> None:
        client, db_path, user = _authed(tmp_path)
        resp = client.post(
            "/api/auth/api-keys",
            json={"name": "ci", "current_password": _TEST_PASSWORD},
            headers=_hdrs(),
        )
        assert resp.status_code == 200
        assert validate_api_key(db_path, resp.json()["key"]) is not None
        assert "current_password" not in resp.text
