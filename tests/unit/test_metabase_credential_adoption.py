"""tests/unit/test_metabase_credential_adoption.py

Verify the credential migration adopts the linked admin's SSO credential from auth.db
when the legacy metabase.yml password is rejected, and never otherwise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
import yaml

import dango.platform.common.metabase_credential_migration as migration
import dango.security.metabase_credentials as credentials
from dango.auth.database import create_user, get_user_by_email
from dango.auth.metabase_sync import decrypt_metabase_password, encrypt_metabase_password
from dango.auth.models import Role, User
from dango.migrations.runner import MigrationRunner
from dango.security.metabase_credentials import MetabaseCredentialStore

EMAIL = "admin@example.com"
LEGACY = "legacy-stale-secret"
LINKED = "linked-working-secret"
URL = "http://localhost:3000"


def _login_post(accepted: set[str]) -> Any:
    """Return a fake ``requests.post`` accepting only the listed passwords."""

    def _post(url: str, json: dict[str, str], timeout: int) -> MagicMock:
        response = MagicMock()
        ok = json["password"] in accepted
        response.status_code = 200 if ok else 401
        response.json.return_value = {"id": f"session-{json['password']}"} if ok else {}
        return response

    return _post


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Project with metabase.yml, project id, a real auth.db and file-fallback stores."""
    root = tmp_path / "project"
    dango_dir = root / ".dango"
    dango_dir.mkdir(parents=True)
    (dango_dir / "metabase.yml").write_text(
        yaml.safe_dump(
            {"metabase_url": URL, "admin": {"email": EMAIL, "password": LEGACY}},
        ),
        encoding="utf-8",
    )
    (dango_dir / "project.yml").write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "T",
                    "id": "adoption-test-id",
                    "created_by": "test@example.com",
                    "purpose": "Unit test",
                }
            }
        ),
        encoding="utf-8",
    )
    migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    MigrationRunner(
        db_path=dango_dir / "auth.db", db_name="auth", migrations_dir=migrations_dir
    ).apply_pending()

    broken_keyring = Mock()
    for name in ("get_password", "set_password", "delete_password"):
        getattr(broken_keyring, name).side_effect = RuntimeError("no keyring")
    monkeypatch.setattr(credentials, "keyring", broken_keyring)
    monkeypatch.setattr("dango.security.token_storage.keyring", broken_keyring)
    monkeypatch.setattr(credentials, "_LOCAL_SECRETS_DIR", tmp_path / "secrets")
    return root


def _link_admin(root: Path, password: str = LINKED) -> User:
    user = User(
        email=EMAIL,
        password_hash="$2b$12$fakehashfakehashfakehashfakehashfakehashfakehashfakeh",
        role=Role.ADMIN,
        metabase_user_id=7,
        metabase_password_enc=encrypt_metabase_password(password, root),
    )
    create_user(root / ".dango" / "auth.db", user)
    return user


def _yml(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((root / ".dango" / "metabase.yml").read_text())
    return data


@pytest.mark.unit
class TestAdoption:
    def test_adopts_linked_admin_credential_when_legacy_password_rejected(
        self, project_root: Path
    ) -> None:
        _link_admin(project_root)
        original = get_user_by_email(project_root / ".dango" / "auth.db", EMAIL)
        assert original is not None
        update = Mock()
        with (
            patch("requests.post", side_effect=_login_post({LINKED})),
            patch("dango.auth.metabase_sync.find_metabase_user_by_email", return_value={"id": 7}),
            patch("dango.auth.metabase_sync.update_metabase_user_password", update),
        ):
            result = migration.complete_metabase_credential_migration(project_root)

        assert result["status"] == "secure_rotated", result
        assert MetabaseCredentialStore(project_root).load() == LINKED
        assert "password" not in _yml(project_root)["admin"]
        update.assert_not_called()
        refreshed = get_user_by_email(project_root / ".dango" / "auth.db", EMAIL)
        assert refreshed is not None and refreshed.metabase_password_enc
        assert refreshed.metabase_password_enc != original.metabase_password_enc
        assert decrypt_metabase_password(refreshed.metabase_password_enc, project_root) == LINKED

    def test_not_adopted_when_linked_credential_also_rejected(self, project_root: Path) -> None:
        _link_admin(project_root)
        with patch("requests.post", side_effect=_login_post(set())):
            result = migration.complete_metabase_credential_migration(project_root)

        assert result["status"] == "failed_non_destructive"
        assert result["reason"] == "current_credential_not_accepted"
        assert _yml(project_root)["admin"]["password"] == LEGACY
        assert MetabaseCredentialStore(project_root).load() is None
        assert MetabaseCredentialStore(project_root).load_pending() is None

    @pytest.mark.parametrize("case", ["no_auth_db", "no_linked_user", "no_ciphertext"])
    def test_not_adopted_without_auth_db_or_without_linked_user_or_ciphertext(
        self, project_root: Path, case: str
    ) -> None:
        db_path = project_root / ".dango" / "auth.db"
        if case == "no_auth_db":
            db_path.unlink()
        elif case == "no_ciphertext":
            create_user(
                db_path,
                User(
                    email=EMAIL,
                    password_hash="$2b$12$fakehashfakehashfakehashfakehashfakehashfakehashfakeh",
                    role=Role.ADMIN,
                ),
            )
        with patch("requests.post", side_effect=_login_post(set())) as post:
            result = migration.complete_metabase_credential_migration(project_root)

        assert result["reason"] == "current_credential_not_accepted"
        assert post.call_count == 1  # only the legacy attempt; no linked-credential login

    def test_legacy_valid_never_touches_auth_db_credential(
        self, project_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _link_admin(project_root)
        linked = Mock(side_effect=AssertionError("must not be called"))
        monkeypatch.setattr(migration, "_linked_admin_session", linked)
        with (
            patch("requests.post", side_effect=_login_post({LEGACY, "candidate"})),
            patch("dango.auth.metabase_sync.generate_metabase_password", return_value="candidate"),
            patch("dango.auth.metabase_sync.update_metabase_user_password", return_value=True),
            patch("dango.auth.metabase_sync.find_metabase_user_by_email", return_value={"id": 7}),
        ):
            result = migration.complete_metabase_credential_migration(project_root)

        assert result["status"] == "secure_rotated"
        linked.assert_not_called()

    @pytest.mark.parametrize(
        ("broken", "reason"),
        [
            ("save_pending", "candidate_staging_failed"),
            ("promote_pending", "pending_promotion_failed"),
        ],
    )
    def test_adoption_failure_paths_are_non_destructive(
        self, project_root: Path, monkeypatch: pytest.MonkeyPatch, broken: str, reason: str
    ) -> None:
        _link_admin(project_root)
        monkeypatch.setattr(MetabaseCredentialStore, broken, Mock(side_effect=OSError("disk full")))
        with patch("requests.post", side_effect=_login_post({LINKED})):
            result = migration.complete_metabase_credential_migration(project_root)

        assert result["status"] == "failed_non_destructive"
        assert result["reason"] == reason
        assert _yml(project_root)["admin"]["password"] == LEGACY

    def test_linked_admin_session_makes_at_most_one_login(self, project_root: Path) -> None:
        _link_admin(project_root)
        with patch("requests.post", side_effect=_login_post({LINKED})) as post:
            adopted = migration._linked_admin_session(project_root, URL, EMAIL)
        assert adopted == (f"session-{LINKED}", LINKED)
        assert post.call_count == 1

        with (
            patch("requests.post", side_effect=_login_post({LINKED})) as post,
            patch(
                "dango.auth.metabase_sync.decrypt_metabase_password",
                side_effect=ValueError("bad ciphertext"),
            ),
        ):
            assert migration._linked_admin_session(project_root, URL, EMAIL) is None
        assert post.call_count == 0
