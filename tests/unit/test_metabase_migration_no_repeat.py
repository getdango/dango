"""tests/unit/test_metabase_migration_no_repeat.py

Verify a permanent credential rejection is remembered (non-secret fingerprint plus time)
so later starts make no failed Metabase login until the inputs change or 24 h pass.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

import dango.platform.common.metabase_credential_migration as migration
import dango.security.metabase_credentials as credentials
from dango.auth.database import create_user, update_user
from dango.auth.metabase_sync import encrypt_metabase_password
from dango.auth.models import Role, User, UserUpdate
from dango.migrations.runner import MigrationRunner

EMAIL = "admin@example.com"
LEGACY = "legacy-stale-secret"
LINKED = "linked-working-secret"
URL = "http://localhost:3000"


def _reject(url: str, json: dict[str, str], timeout: int) -> MagicMock:
    return MagicMock(status_code=401)


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    dango_dir = root / ".dango"
    dango_dir.mkdir(parents=True)
    (dango_dir / "metabase.yml").write_text(
        yaml.safe_dump({"metabase_url": URL, "admin": {"email": EMAIL, "password": LEGACY}}),
        encoding="utf-8",
    )
    (dango_dir / "project.yml").write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "T",
                    "id": "no-repeat-test-id",
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
    broken = MagicMock()
    for name in ("get_password", "set_password", "delete_password"):
        getattr(broken, name).side_effect = RuntimeError("no keyring")
    monkeypatch.setattr(credentials, "keyring", broken)
    monkeypatch.setattr("dango.security.token_storage.keyring", broken)
    monkeypatch.setattr(credentials, "_LOCAL_SECRETS_DIR", tmp_path / "secrets")
    create_user(
        dango_dir / "auth.db",
        User(
            email=EMAIL,
            password_hash="$2b$12$fakehashfakehashfakehashfakehashfakehashfakehashfakeh",
            role=Role.ADMIN,
            metabase_user_id=7,
            metabase_password_enc=encrypt_metabase_password(LINKED, root),
        ),
    )
    return root


def _state(root: Path) -> dict[str, Any]:
    path = root / ".dango" / "state" / "metabase_credential_migration.json"
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _fail_once(root: Path) -> dict[str, object]:
    with patch("requests.post", side_effect=_reject):
        return migration.complete_metabase_credential_migration(root)


@pytest.mark.unit
class TestNoRepeatedFailedLogins:
    def test_permanent_failure_records_fingerprint_and_time(self, project_root: Path) -> None:
        result = _fail_once(project_root)

        assert result["reason"] == "current_credential_not_accepted"
        state = _state(project_root)
        assert len(state["inputs"]) == 16
        assert abs(datetime.fromisoformat(state["at"]) - datetime.now(timezone.utc)) < timedelta(
            minutes=1
        )

    def test_transient_failure_records_no_fingerprint(self, project_root: Path) -> None:
        state = migration._failed(project_root, "sso_refresh_failed")
        assert "inputs" not in state and "at" not in state

    @pytest.mark.parametrize("failure", ["http_503", "http_429", "timeout"])
    def test_transient_login_failure_is_retryable_and_never_remembered(
        self, project_root: Path, failure: str
    ) -> None:
        def _transient(url: str, json: dict[str, str], timeout: int) -> MagicMock:
            if failure == "timeout":
                raise TimeoutError("slow")
            return MagicMock(status_code=503 if failure == "http_503" else 429)

        with patch("requests.post", side_effect=_transient):
            result = migration.complete_metabase_credential_migration(project_root)

        assert result["reason"] == "login_unavailable"
        assert "inputs" not in _state(project_root)
        with patch("requests.post", side_effect=_reject) as post:
            migration.complete_metabase_credential_migration(project_root)
        assert post.call_count == 2  # not skipped: the next start tries again

    def test_second_call_with_unchanged_inputs_makes_no_login_attempt(
        self, project_root: Path
    ) -> None:
        _fail_once(project_root)
        before = _state(project_root)

        with patch("requests.post", side_effect=AssertionError("login attempted")):
            result = migration.complete_metabase_credential_migration(project_root)

        assert result["skipped"] is True
        assert result["reason"] == "current_credential_not_accepted"
        assert _state(project_root) == before  # no state write

    @pytest.mark.parametrize("change", ["metabase_yml", "sso_ciphertext"])
    def test_changed_metabase_yml_or_sso_ciphertext_retries_once(
        self, project_root: Path, change: str
    ) -> None:
        _fail_once(project_root)
        if change == "metabase_yml":
            path = project_root / ".dango" / "metabase.yml"
            path.write_text(path.read_text() + "# touched\n", encoding="utf-8")
        else:
            update_user(
                project_root / ".dango" / "auth.db",
                _admin_id(project_root),
                UserUpdate(metabase_password_enc=encrypt_metabase_password("new", project_root)),
            )

        with patch("requests.post", side_effect=_reject) as post:
            result = migration.complete_metabase_credential_migration(project_root)

        assert "skipped" not in result
        assert result["reason"] == "current_credential_not_accepted"
        assert post.call_count == 2  # legacy + linked, once each

    def test_retries_after_24_hours(self, project_root: Path) -> None:
        _fail_once(project_root)
        state = _state(project_root)
        state["at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        migration._write_state(project_root, state)

        with patch("requests.post", side_effect=_reject) as post:
            result = migration.complete_metabase_credential_migration(project_root)

        assert "skipped" not in result
        assert post.call_count == 2

    def test_fingerprint_contains_no_password(self, project_root: Path) -> None:
        _fail_once(project_root)
        text = (
            project_root / ".dango" / "state" / "metabase_credential_migration.json"
        ).read_text()
        assert LEGACY not in text
        assert LINKED not in text

    def test_success_after_skip_window_clears_the_failure(self, project_root: Path) -> None:
        _fail_once(project_root)
        state = _state(project_root)
        state["at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        migration._write_state(project_root, state)

        def _accept_linked(url: str, json: dict[str, str], timeout: int) -> MagicMock:
            ok = json["password"] == LINKED
            response = MagicMock(status_code=200 if ok else 401)
            response.json.return_value = {"id": "s"} if ok else {}
            return response

        with (
            patch("requests.post", side_effect=_accept_linked),
            patch("dango.auth.metabase_sync.find_metabase_user_by_email", return_value={"id": 7}),
        ):
            result = migration.complete_metabase_credential_migration(project_root)

        assert result["status"] == "secure_rotated"
        assert _state(project_root) == {"version": 1, "status": "secure_rotated"}


def _admin_id(root: Path) -> str:
    from dango.auth.database import get_user_by_email

    user = get_user_by_email(root / ".dango" / "auth.db", EMAIL)
    assert user is not None
    return user.id
