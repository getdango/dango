"""tests/unit/test_metabase_credential_store.py

Focused unit tests for the isolated Metabase administrator credential store.
"""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

import dango.security.metabase_credentials as credentials
from dango.security import MetabaseCredentialStore, MetabaseCredentialStoreError

PROJECT_ID = "project-credential-store-test"


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    dango_dir = project / ".dango"
    dango_dir.mkdir(parents=True)
    (dango_dir / "project.yml").write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "Credential Store Test",
                    "id": PROJECT_ID,
                    "created_by": "test@example.com",
                    "purpose": "Credential store unit test",
                }
            }
        ),
        encoding="utf-8",
    )
    return project


@pytest.fixture
def local_fallback_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "operator-home" / ".dango" / "secrets" / "metabase"
    monkeypatch.setattr(credentials, "_LOCAL_SECRETS_DIR", path)
    return path


@pytest.mark.unit
class TestMetabaseCredentialStore:
    def test_keyring_round_trip_does_not_create_fallback(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path, local_fallback_dir: Path
    ) -> None:
        keyring = Mock()
        credentials_by_project: dict[str, str] = {}
        keyring.set_password.side_effect = lambda service, username, password: (
            credentials_by_project.__setitem__(username, password)
        )
        keyring.get_password.side_effect = lambda service, username: credentials_by_project.get(
            username
        )
        monkeypatch.setattr(credentials, "keyring", keyring)

        store = MetabaseCredentialStore(project_root, cloud_mode=False)
        store.save("correct horse battery staple")

        assert store.load() == "correct horse battery staple"
        assert store.secret_path is None
        assert not local_fallback_dir.exists()
        keyring.set_password.assert_called_once_with(
            "dango-metabase", PROJECT_ID, "correct horse battery staple"
        )

    def test_keyring_failure_uses_path_outside_project(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path, local_fallback_dir: Path
    ) -> None:
        keyring = Mock()
        keyring.set_password.side_effect = RuntimeError("keychain unavailable")
        keyring.get_password.side_effect = RuntimeError("keychain unavailable")
        monkeypatch.setattr(credentials, "keyring", keyring)

        store = MetabaseCredentialStore(project_root, cloud_mode=False)
        store.save("fallback-secret")

        assert store.secret_path == local_fallback_dir / f"{PROJECT_ID}.json"
        assert store.load() == "fallback-secret"
        assert store.secret_path is not None
        assert project_root not in store.secret_path.parents
        assert not list(project_root.rglob("*.json"))

    def test_cloud_uses_cloud_path_without_keyring(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path, tmp_path: Path
    ) -> None:
        cloud_dir = tmp_path / "srv" / "dango" / "secrets" / "metabase"
        monkeypatch.setattr(credentials, "_CLOUD_SECRETS_DIR", cloud_dir)
        keyring = Mock()
        monkeypatch.setattr(credentials, "keyring", keyring)

        store = MetabaseCredentialStore(project_root, cloud_mode=True)
        store.save("cloud-secret")

        assert store.secret_path == cloud_dir / f"{PROJECT_ID}.json"
        assert store.load() == "cloud-secret"
        keyring.get_password.assert_not_called()
        keyring.set_password.assert_not_called()

    def test_missing_credential_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path
    ) -> None:
        keyring = Mock()
        keyring.get_password.return_value = ""
        monkeypatch.setattr(credentials, "keyring", keyring)

        store = MetabaseCredentialStore(project_root, cloud_mode=False)

        assert store.load() is None
        assert store.secret_path is None

    def test_fallback_permissions_are_restricted(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path, local_fallback_dir: Path
    ) -> None:
        keyring = Mock()
        keyring.set_password.side_effect = RuntimeError("keychain unavailable")
        monkeypatch.setattr(credentials, "keyring", keyring)

        store = MetabaseCredentialStore(project_root, cloud_mode=False)
        store.save("fallback-secret")

        assert store.secret_path is not None
        assert stat.S_IMODE(store.secret_path.stat().st_mode) == 0o600
        for directory in (
            local_fallback_dir,
            local_fallback_dir.parent,
            local_fallback_dir.parent.parent,
        ):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_malformed_fallback_file_raises_specific_error(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path, local_fallback_dir: Path
    ) -> None:
        keyring = Mock()
        keyring.get_password.side_effect = RuntimeError("keychain unavailable")
        monkeypatch.setattr(credentials, "keyring", keyring)
        local_fallback_dir.mkdir(parents=True)
        (local_fallback_dir / f"{PROJECT_ID}.json").write_text("not-json", encoding="utf-8")

        with pytest.raises(MetabaseCredentialStoreError, match="malformed"):
            MetabaseCredentialStore(project_root, cloud_mode=False).load()

    def test_missing_persisted_project_id_fails_without_writing_secret(
        self,
        monkeypatch: pytest.MonkeyPatch,
        project_root: Path,
        local_fallback_dir: Path,
    ) -> None:
        project_file = project_root / ".dango" / "project.yml"
        config = yaml.safe_load(project_file.read_text(encoding="utf-8"))
        del config["project"]["id"]
        project_file.write_text(yaml.safe_dump(config), encoding="utf-8")
        monkeypatch.setattr(credentials, "keyring", Mock())

        with pytest.raises(MetabaseCredentialStoreError, match="persisted project id"):
            MetabaseCredentialStore(project_root, cloud_mode=False)

        assert not local_fallback_dir.exists()
        assert PROJECT_ID not in project_file.read_text(encoding="utf-8")
