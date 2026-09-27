"""tests/unit/test_metabase_config.py

Unit tests for the Metabase metadata and credential access boundary.
"""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

import dango.security.metabase_credentials as credentials
from dango.security import MetabaseCredentialStoreError
from dango.visualization.metabase_config import (
    MetabaseConfigurationError,
    load_metabase_admin_credentials,
    load_metabase_metadata,
    write_metabase_metadata,
)

PROJECT_ID = "project-metabase-config-test"


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    dango_dir = project / ".dango"
    dango_dir.mkdir(parents=True)
    (dango_dir / "project.yml").write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "Config Test",
                    "id": PROJECT_ID,
                    "created_by": "test@example.com",
                    "purpose": "Metabase configuration unit test",
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


def _write_metadata(project_root: Path, metadata: dict[str, object]) -> None:
    (project_root / ".dango" / "metabase.yml").write_text(
        yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8"
    )


@pytest.mark.unit
class TestMetabaseConfig:
    def test_load_admin_credentials_prefers_protected_secret(
        self,
        monkeypatch: pytest.MonkeyPatch,
        project_root: Path,
        local_fallback_dir: Path,
    ) -> None:
        _write_metadata(
            project_root,
            {"admin": {"email": "admin@example.com", "password": "legacy-password"}},
        )
        keyring = Mock()
        keyring.get_password.return_value = "protected-password"
        monkeypatch.setattr(credentials, "keyring", keyring)

        assert load_metabase_admin_credentials(project_root) == (
            "admin@example.com",
            "protected-password",
        )
        assert not local_fallback_dir.exists()

    def test_load_admin_credentials_uses_legacy_secret_only_when_store_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
        project_root: Path,
    ) -> None:
        _write_metadata(
            project_root,
            {"admin": {"email": "admin@example.com", "password": "legacy-password"}},
        )
        keyring = Mock()
        keyring.get_password.return_value = None
        monkeypatch.setattr(credentials, "keyring", keyring)

        assert load_metabase_admin_credentials(project_root) == (
            "admin@example.com",
            "legacy-password",
        )

    def test_load_admin_credentials_uses_legacy_secret_without_persisted_project_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        project_root: Path,
        local_fallback_dir: Path,
    ) -> None:
        project_file = project_root / ".dango" / "project.yml"
        project = yaml.safe_load(project_file.read_text(encoding="utf-8"))
        del project["project"]["id"]
        project_file.write_text(yaml.safe_dump(project), encoding="utf-8")
        _write_metadata(
            project_root,
            {"admin": {"email": "admin@example.com", "password": "legacy-password"}},
        )
        keyring = Mock()
        monkeypatch.setattr(credentials, "keyring", keyring)

        assert load_metabase_admin_credentials(project_root) == (
            "admin@example.com",
            "legacy-password",
        )
        assert not local_fallback_dir.exists()
        keyring.get_password.assert_not_called()

    def test_load_admin_credentials_raises_for_malformed_protected_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        project_root: Path,
        local_fallback_dir: Path,
    ) -> None:
        _write_metadata(
            project_root,
            {"admin": {"email": "admin@example.com", "password": "legacy-password"}},
        )
        local_fallback_dir.mkdir(parents=True)
        (local_fallback_dir / f"{PROJECT_ID}.json").write_text("not-json", encoding="utf-8")
        keyring = Mock()
        keyring.get_password.side_effect = RuntimeError("keychain unavailable")
        monkeypatch.setattr(credentials, "keyring", keyring)

        with pytest.raises(MetabaseCredentialStoreError, match="malformed"):
            load_metabase_admin_credentials(project_root)

    @pytest.mark.parametrize(
        "metadata",
        [
            {"admin": {"password": "legacy-password"}},
            {"admin": {"email": "admin@example.com"}},
            {"admin": {"email": "", "password": "legacy-password"}},
        ],
    )
    def test_load_admin_credentials_returns_none_without_email_or_password(
        self,
        monkeypatch: pytest.MonkeyPatch,
        project_root: Path,
        metadata: dict[str, object],
    ) -> None:
        _write_metadata(project_root, metadata)
        keyring = Mock()
        keyring.get_password.return_value = None
        monkeypatch.setattr(credentials, "keyring", keyring)

        assert load_metabase_admin_credentials(project_root) is None

    def test_write_metadata_rejects_password(self, project_root: Path) -> None:
        with pytest.raises(MetabaseConfigurationError, match="must not contain"):
            write_metabase_metadata(
                project_root,
                {"admin": {"email": "admin@example.com", "password": "do-not-write"}},
            )

        assert not (project_root / ".dango" / "metabase.yml").exists()

    def test_write_metadata_round_trip_preserves_nonsecret_fields_and_mode(
        self, project_root: Path
    ) -> None:
        metadata: dict[str, object] = {
            "metabase_url": "http://localhost:3000",
            "admin": {"email": "admin@example.com"},
            "database": {"id": 5, "name": "Analytics"},
            "site_url_set": True,
            "setup_completed_at": "2026-09-27T00:00:00+00:00",
        }

        write_metabase_metadata(project_root, metadata)

        path = project_root / ".dango" / "metabase.yml"
        assert load_metabase_metadata(project_root) == metadata
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_malformed_metadata_raises_specific_error(self, project_root: Path) -> None:
        (project_root / ".dango" / "metabase.yml").write_text("admin: [", encoding="utf-8")

        with pytest.raises(MetabaseConfigurationError, match="malformed"):
            load_metabase_metadata(project_root)
