"""tests/unit/test_keychain_metabase_credential_continuity.py

The Metabase admin password must be found wherever it was written (keychain or fallback file) when the OS keychain answers differently between runs, using the real MetabaseCredentialStore and a scripted keyring.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import keyring
import pytest
import yaml

import dango.security.metabase_credentials as credentials
from dango.security import MetabaseCredentialStore
from tests.factories.scripted_keyring import ScriptedKeyring

SERVICE = "dango-metabase"
PW1 = "planted-admin-pw-one-5d2e"
PW2 = "planted-admin-pw-two-c8a1"


def _write_project(root: Path, project_id: str) -> Path:
    (root / ".dango").mkdir(parents=True)
    (root / ".dango" / "project.yml").write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "Continuity",
                    "id": project_id,
                    "created_by": "test@example.com",
                    "purpose": "Keychain continuity",
                }
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def kr(_isolate_keyring) -> ScriptedKeyring:
    backend = ScriptedKeyring()
    keyring.set_keyring(backend)
    return backend


@pytest.fixture
def fallback_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "home" / ".dango" / "secrets" / "metabase"
    monkeypatch.setattr(credentials, "_LOCAL_SECRETS_DIR", path)
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return _write_project(tmp_path / "project", "continuity-project")


def _store(root: Path) -> MetabaseCredentialStore:
    return MetabaseCredentialStore(root, cloud_mode=False)


def _file(fallback_dir: Path, name: str = "continuity-project.json") -> Path:
    return fallback_dir / name


@pytest.mark.unit
class TestCredentialFoundWhereverItWasWritten:
    @pytest.mark.parametrize("mode", ["deny", "locked", "secauth", "windows", "missing"])
    def test_saved_while_unavailable_is_found_once_the_keychain_answers(
        self, kr, root, fallback_dir, mode
    ):
        kr.mode = mode
        _store(root).save(PW1)
        assert _file(fallback_dir).exists()
        kr.mode = "allow"

        assert _store(root).load() == PW1

    def test_keychain_value_wins_when_it_exists(self, kr, root, fallback_dir):
        kr.mode = "deny"
        _store(root).save(PW1)
        kr.mode = "allow"
        kr.store[(SERVICE, "continuity-project")] = PW2

        assert _store(root).load() == PW2

    def test_reading_never_writes_or_deletes(self, kr, root, fallback_dir):
        kr.mode = "deny"
        _store(root).save(PW1)
        before = _file(fallback_dir).read_bytes()
        kr.ops.clear()
        kr.mode = "allow"

        _store(root).load()

        assert kr.writes() == []
        assert _file(fallback_dir).read_bytes() == before


@pytest.mark.unit
class TestSupersededFallbackIsRemoved:
    def test_keychain_save_removes_the_stale_fallback_copy(self, kr, root, fallback_dir):
        kr.mode = "deny"
        _store(root).save(PW1)
        kr.mode = "allow"
        _store(root).save(PW2)
        assert not _file(fallback_dir).exists()
        kr.mode = "deny"

        assert _store(root).load() != PW1  # a stale password is never returned silently

    def test_fallback_copy_is_kept_when_the_keychain_did_not_keep_the_value(
        self, kr, root, fallback_dir
    ):
        kr.mode = "deny"
        _store(root).save(PW1)
        kr.mode = "drop_writes"  # set "succeeds" but nothing is stored

        _store(root).save(PW2)

        assert _file(fallback_dir).exists()
        kr.mode = "allow"
        assert _store(root).load() == PW1

    def test_delete_removes_both_copies(self, kr, root, fallback_dir):
        # The 1.0.14 layout left both copies behind after deny, then allow.
        kr.store[(SERVICE, "continuity-project")] = PW1
        fallback_dir.mkdir(parents=True)
        _file(fallback_dir).write_text(json.dumps({"password": PW1}), encoding="utf-8")

        _store(root).delete()

        assert kr.store == {}
        assert not _file(fallback_dir).exists()
        kr.mode = "deny"
        assert _store(root).load() is None

    def test_discard_pending_removes_both_copies(self, kr, root, fallback_dir):
        kr.store[(SERVICE, "continuity-project:pending")] = PW1
        fallback_dir.mkdir(parents=True)
        pending = _file(fallback_dir, "continuity-project.pending.json")
        pending.write_text(json.dumps({"password": PW1}), encoding="utf-8")

        _store(root).discard_pending()

        assert kr.store == {}
        assert not pending.exists()


@pytest.mark.unit
class TestCharacterisation:
    def test_locked_keychain_without_a_fallback_file_reports_no_credential(
        self, kr, root, fallback_dir
    ):
        _store(root).save(PW1)
        kr.mode = "locked"
        kr.ops.clear()

        assert _store(root).load() is None
        assert kr.writes() == []
        assert not fallback_dir.exists(), "reading while locked creates nothing"

    def test_missing_backend_is_a_permanent_file_store(self, kr, root, fallback_dir):
        kr.mode = "missing"
        store = _store(root)
        store.save(PW1)

        assert _store(root).load() == PW1
        assert store.secret_path == _file(fallback_dir)
        assert kr.store == {}

    def test_two_projects_never_see_each_others_credentials(self, kr, root, fallback_dir, tmp_path):
        other = _write_project(tmp_path / "other", "other-project")
        _store(root).save(PW1)
        MetabaseCredentialStore(other, cloud_mode=False).save(PW2)
        assert _store(root).load() == PW1
        assert MetabaseCredentialStore(other, cloud_mode=False).load() == PW2

        kr.mode = "missing"  # file store: one file per project id
        _store(root).save(PW2)
        MetabaseCredentialStore(other, cloud_mode=False).save(PW1)
        assert _store(root).load() == PW2
        assert MetabaseCredentialStore(other, cloud_mode=False).load() == PW1

    def test_service_and_username_are_unchanged(self, kr, root, fallback_dir):
        store = _store(root)
        store.save(PW1)
        store.save_pending(PW2)
        assert set(kr.store) == {
            (SERVICE, "continuity-project"),
            (SERVICE, "continuity-project:pending"),
        }


@pytest.mark.unit
class TestUpgradeFromThe1014Layout:
    def test_fallback_file_written_by_1014_is_read_and_left_untouched(self, kr, root, fallback_dir):
        fallback_dir.mkdir(parents=True)
        path = _file(fallback_dir)
        path.write_text(json.dumps({"password": PW1}), encoding="utf-8")
        before = path.read_bytes()

        for mode in ("allow", "deny", "missing"):
            kr.mode = mode
            assert _store(root).load() == PW1

        assert path.read_bytes() == before
        assert kr.writes() == []

    def test_keychain_entry_written_by_1014_is_read_without_writes(self, kr, root, fallback_dir):
        kr.store[(SERVICE, "continuity-project")] = PW1

        assert _store(root).load() == PW1
        assert kr.writes() == []
        assert not fallback_dir.exists()


@pytest.mark.unit
class TestNothingSecretIsShown:
    def test_no_password_in_output_logs_or_errors(self, kr, root, fallback_dir, capsys, caplog):
        caplog.set_level(logging.DEBUG)
        seen: list[str] = []
        for mode in ("deny", "locked", "windows", "missing", "allow"):
            kr.mode = mode
            store = _store(root)
            store.save(PW1)
            store.save_pending(PW2)
            store.load()
            store.load_pending()
            store.discard_pending()
        _file(fallback_dir).write_text('{"password": 7, "x": "' + PW1 + '"}', encoding="utf-8")
        kr.mode = "deny"
        with pytest.raises(credentials.MetabaseCredentialStoreError) as excinfo:
            _store(root).load()
        seen.append(str(excinfo.value))

        captured = capsys.readouterr()
        shown = captured.out + captured.err + caplog.text + "".join(seen)
        assert PW1 not in shown and PW2 not in shown
