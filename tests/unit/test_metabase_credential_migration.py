"""tests/unit/test_metabase_credential_migration.py

Verify crash-safe migration of legacy Metabase administrator credentials.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

import dango.platform.common.metabase_credential_migration as migration


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    dango_dir = root / ".dango"
    dango_dir.mkdir(parents=True)
    (dango_dir / "metabase.yml").write_text(
        yaml.safe_dump(
            {
                "metabase_url": "http://localhost:3000",
                "admin": {"email": "admin@example.com", "password": "legacy-secret"},
            }
        ),
        encoding="utf-8",
    )
    (dango_dir / "project.yml").write_text(
        yaml.safe_dump({"project": {"name": "Test", "id": "migration-test-id"}}),
        encoding="utf-8",
    )
    return root


class FakeStore:
    """In-memory protected store with an event log for ordering assertions."""

    def __init__(self, _root: Path, events: list[str], active: str | None = None) -> None:
        self.events = events
        self.active = active
        self.pending: str | None = None

    def load(self) -> str | None:
        self.events.append("load_active")
        return self.active

    def load_pending(self) -> str | None:
        self.events.append("load_pending")
        return self.pending

    def save_pending(self, password: str) -> None:
        self.events.append("stage")
        self.pending = password

    def promote_pending(self) -> str:
        self.events.append("promote")
        assert self.pending is not None
        self.active = self.pending
        self.pending = None
        return self.active

    def discard_pending(self) -> None:
        self.events.append("discard")
        self.pending = None


@pytest.mark.unit
class TestMetabaseCredentialMigration:
    def test_prepare_is_offline_and_works_without_project_id(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path
    ) -> None:
        project_file = project_root / ".dango" / "project.yml"
        project_file.write_text(yaml.safe_dump({"project": {"name": "Test"}}), encoding="utf-8")
        monkeypatch.setattr(
            migration,
            "MetabaseCredentialStore",
            lambda _root: pytest.fail("prepare must not instantiate credential storage"),
        )

        result = migration.prepare_metabase_credential_migration(project_root)

        assert result == {"version": 1, "status": "prepared"}
        state = json.loads(
            (project_root / ".dango" / "state" / "metabase_credential_migration.json").read_text()
        )
        assert state == result
        assert "legacy-secret" not in json.dumps(state)

    def test_successful_order_stages_verifies_promotes_refreshes_and_cleans(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path
    ) -> None:
        events: list[str] = []
        store = FakeStore(project_root, events)
        monkeypatch.setattr(migration, "MetabaseCredentialStore", lambda _root: store)
        monkeypatch.setattr(
            migration,
            "_create_session",
            lambda _url, _email, password: (
                events.append(f"login:{password}")
                or ("candidate-session" if password == "candidate" else "old-session")
            ),
        )
        monkeypatch.setattr(migration, "_find_metabase_user", lambda *_args: {"id": 9})
        monkeypatch.setattr(
            "dango.auth.metabase_sync.generate_metabase_password", lambda: "candidate"
        )
        monkeypatch.setattr(
            "dango.auth.metabase_sync.update_metabase_user_password",
            lambda *_args, **_kwargs: events.append("remote_update") or True,
        )
        monkeypatch.setattr(
            migration,
            "_refresh_linked_sso_passwords",
            lambda *_args: events.append("sso_refresh"),
        )
        original_write = migration.write_metabase_metadata
        monkeypatch.setattr(
            migration,
            "write_metabase_metadata",
            lambda root, metadata: (
                events.append("metadata_cleanup") or original_write(root, metadata)
            ),
        )

        result = migration.complete_metabase_credential_migration(project_root)

        assert result["status"] == "secure_rotated"
        ordered = [
            event
            for event in events
            if event
            in {
                "stage",
                "remote_update",
                "login:candidate",
                "promote",
                "sso_refresh",
                "metadata_cleanup",
            }
        ]
        assert ordered == [
            "stage",
            "remote_update",
            "login:candidate",
            "promote",
            "sso_refresh",
            "metadata_cleanup",
        ]
        metadata = yaml.safe_load((project_root / ".dango" / "metabase.yml").read_text())
        assert "password" not in metadata["admin"]

    def test_pre_rotation_store_or_login_failure_never_mutates_remote_or_yaml(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path
    ) -> None:
        events: list[str] = []
        store = FakeStore(project_root, events)
        monkeypatch.setattr(migration, "MetabaseCredentialStore", lambda _root: store)
        monkeypatch.setattr(migration, "_create_session", lambda *_args: None)
        remote_update = Mock(return_value=True)
        monkeypatch.setattr("dango.auth.metabase_sync.update_metabase_user_password", remote_update)

        result = migration.complete_metabase_credential_migration(project_root)

        assert result["status"] == "failed_non_destructive"
        assert "stage" not in events
        remote_update.assert_not_called()
        assert "legacy-secret" in (project_root / ".dango" / "metabase.yml").read_text()

    def test_pre_rotation_store_failure_never_mutates_yaml(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path
    ) -> None:
        class UnavailableStore:
            def __init__(self, _root: Path) -> None:
                pass

            def load(self) -> str | None:
                raise RuntimeError("keyring unavailable")

        monkeypatch.setattr(migration, "MetabaseCredentialStore", UnavailableStore)

        result = migration.complete_metabase_credential_migration(project_root)

        assert result == {
            "version": 1,
            "status": "failed_non_destructive",
            "reason": "protected_store_unavailable",
        }
        assert "legacy-secret" in (project_root / ".dango" / "metabase.yml").read_text()

    def test_ambiguous_remote_result_with_working_candidate_recovers_without_second_rotation(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path
    ) -> None:
        events: list[str] = []
        store = FakeStore(project_root, events)
        store.pending = "candidate"
        monkeypatch.setattr(migration, "MetabaseCredentialStore", lambda _root: store)
        monkeypatch.setattr(
            migration,
            "_create_session",
            lambda _url, _email, password: "candidate-session" if password == "candidate" else None,
        )
        monkeypatch.setattr(migration, "_find_metabase_user", lambda *_args: {"id": 9})
        monkeypatch.setattr(migration, "_refresh_linked_sso_passwords", lambda *_args: None)
        remote_update = Mock(return_value=False)
        monkeypatch.setattr("dango.auth.metabase_sync.update_metabase_user_password", remote_update)

        result = migration.complete_metabase_credential_migration(project_root)

        assert result["status"] == "secure_rotated"
        remote_update.assert_not_called()
        assert store.active == "candidate"

    def test_promoted_but_not_cleaned_state_resumes_without_another_rotation(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path
    ) -> None:
        events: list[str] = []
        store = FakeStore(project_root, events, active="candidate")
        monkeypatch.setattr(migration, "MetabaseCredentialStore", lambda _root: store)
        monkeypatch.setattr(migration, "_create_session", lambda *_args: "active-session")
        monkeypatch.setattr(migration, "_find_metabase_user", lambda *_args: {"id": 9})
        monkeypatch.setattr(migration, "_refresh_linked_sso_passwords", lambda *_args: None)
        remote_update = Mock(return_value=True)
        monkeypatch.setattr("dango.auth.metabase_sync.update_metabase_user_password", remote_update)

        result = migration.complete_metabase_credential_migration(project_root)

        assert result["status"] == "secure_rotated"
        assert "stage" not in events
        remote_update.assert_not_called()

    def test_password_free_metadata_is_idempotent(self, project_root: Path) -> None:
        (project_root / ".dango" / "metabase.yml").write_text(
            yaml.safe_dump({"admin": {"email": "admin@example.com"}}), encoding="utf-8"
        )

        assert migration.prepare_metabase_credential_migration(project_root) == {
            "status": "not_required"
        }
        assert migration.complete_metabase_credential_migration(project_root) == {
            "status": "not_required"
        }

    def test_refresh_linked_sso_password_updates_only_existing_link(
        self, monkeypatch: pytest.MonkeyPatch, project_root: Path
    ) -> None:
        linked = SimpleNamespace(id="linked", metabase_user_id=9)
        unrelated = SimpleNamespace(id="other", metabase_user_id=10)
        monkeypatch.setattr(
            "dango.auth.admin.get_auth_db_path", lambda _root: project_root / ".dango" / "auth.db"
        )
        (project_root / ".dango" / "auth.db").touch()
        monkeypatch.setattr(
            "dango.auth.database.list_users", lambda *_args, **_kwargs: [linked, unrelated]
        )
        monkeypatch.setattr(
            "dango.auth.metabase_sync.encrypt_metabase_password",
            lambda *_args: "encrypted-candidate",
        )
        update_user = Mock()
        monkeypatch.setattr("dango.auth.database.update_user", update_user)

        migration._refresh_linked_sso_passwords(project_root, 9, "candidate")

        update_user.assert_called_once()
        assert update_user.call_args.args[:2] == (project_root / ".dango" / "auth.db", "linked")
        assert update_user.call_args.args[2].metabase_password_enc == "encrypted-candidate"
