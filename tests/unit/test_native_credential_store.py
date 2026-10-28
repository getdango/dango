"""tests/unit/test_native_credential_store.py

Exercise isolated native credential reference, promotion and filesystem boundaries.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from pydantic import ValidationError

import dango.security.native_credentials as credentials
from dango.security.native_credentials import (
    NativeCredentialStore,
    NativeCredentialStoreError,
    SecretRef,
)

pytestmark = pytest.mark.unit
CANARY = "synthetic-native-secret-canary"
PROJECT = "project-native-test"
ACTOR = "actor-one"


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Mock]:
    project = tmp_path / "project"
    (project / ".dango").mkdir(parents=True)
    (project / ".dango/project.yml").write_text(
        yaml.safe_dump({"project": {"id": PROJECT}}), encoding="utf-8"
    )
    root = tmp_path / "home/.dango/secrets/native"
    monkeypatch.setattr(credentials, "_LOCAL_SECRETS_DIR", root)
    monkeypatch.setattr(credentials, "_CLOUD_SECRETS_DIR", root)
    backend = Mock()
    values: dict[tuple[str, str], str] = {}
    backend.set_password.side_effect = lambda service, key, value: values.__setitem__(
        (service, key), value
    )
    backend.get_password.side_effect = lambda service, key: values.get((service, key))
    backend.delete_password.side_effect = lambda service, key: values.pop((service, key), None)
    monkeypatch.setattr(credentials, "keyring", backend)
    return project, root, backend


def _files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def test_keyring_preferred_without_project_secret(sandbox: tuple[Path, Path, Mock]) -> None:
    project, root, backend = sandbox
    before = (project / ".dango/project.yml").read_bytes()
    store = NativeCredentialStore(project, cloud_mode=False)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    assert store.load(ref) == {"token": CANARY}
    assert NativeCredentialStore(project, cloud_mode=False).load(ref) == {"token": CANARY}
    assert (project / ".dango/project.yml").read_bytes() == before
    assert [
        path.relative_to(project).as_posix() for path in project.rglob("*") if path.is_file()
    ] == [".dango/project.yml"]
    assert {call.args[0] for call in backend.set_password.call_args_list} == {"dango-native"}
    assert all(CANARY not in path.read_text() for path in _files(root))
    assert set(ref.model_dump(mode="json")) == {
        "project_id",
        "actor_id",
        "credential_id",
        "version",
    }


def test_cloud_external_owner_only_fallback(sandbox: tuple[Path, Path, Mock]) -> None:
    project, root, backend = sandbox
    store = NativeCredentialStore(project, cloud_mode=True)
    ref = store.save(ACTOR, {"password": CANARY}, "v1")
    assert store.load(ref) == {"password": CANARY}
    assert not backend.mock_calls
    assert project not in root.parents
    for path in (root, *root.rglob("*")):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert any(CANARY in path.read_text() for path in _files(root))
    assert all(ACTOR not in str(path) and PROJECT not in str(path) for path in _files(root))


def test_local_unavailable_keyring_pins_fallback(sandbox: tuple[Path, Path, Mock]) -> None:
    project, _, backend = sandbox
    backend.set_password.side_effect = RuntimeError(CANARY)
    store = NativeCredentialStore(project, cloud_mode=False)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    backend.get_password.reset_mock()
    assert store.load(ref) == {"token": CANARY}
    backend.get_password.assert_not_called()


def test_wrong_project_actor_or_version_denied(sandbox: tuple[Path, Path, Mock]) -> None:
    project, _, backend = sandbox
    store = NativeCredentialStore(project, cloud_mode=False)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    backend.get_password.reset_mock()
    with pytest.raises(NativeCredentialStoreError):
        store.load(ref.model_copy(update={"project_id": "foreign-project"}))
    for changes in ({"actor_id": "other-actor"}, {"version": "v2"}, {"credential_id": "unknown"}):
        assert store.load(ref.model_copy(update=changes)) is None
    backend.get_password.assert_not_called()
    # Storage does not authenticate the holder of an intact reference.
    assert store.load(ref) == {"token": CANARY}


def test_project_identity_rechecked_before_lookup(sandbox: tuple[Path, Path, Mock]) -> None:
    project, _, backend = sandbox
    store = NativeCredentialStore(project, cloud_mode=False)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    (project / ".dango/project.yml").write_text("project:\n  id: changed\n")
    backend.get_password.reset_mock()
    with pytest.raises(NativeCredentialStoreError):
        store.load(ref)
    backend.get_password.assert_not_called()


@pytest.mark.parametrize("stage", ["candidate", "record"])
def test_atomic_pending_promote_failure_preserves_active(
    sandbox: tuple[Path, Path, Mock], monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    project, _, _ = sandbox
    store = NativeCredentialStore(project, cloud_mode=True)
    active = store.save(ACTOR, {"token": "old-synthetic"}, "v1")
    original = credentials.os.replace

    def fail(source: str, target: str, **kwargs: object) -> None:
        if (target == "record.json") == (stage == "record"):
            raise OSError(CANARY)
        original(source, target, **kwargs)

    monkeypatch.setattr(credentials.os, "replace", fail)
    with pytest.raises(NativeCredentialStoreError):
        store.save(ACTOR, {"token": CANARY}, "v2")
    assert store.load(active) == {"token": "old-synthetic"}


def test_keyring_candidate_validation_failure_preserves_active(
    sandbox: tuple[Path, Path, Mock],
) -> None:
    project, _, backend = sandbox
    store = NativeCredentialStore(project, cloud_mode=False)
    active = store.save(ACTOR, {"token": "old-synthetic"}, "v1")
    original = backend.get_password.side_effect
    backend.get_password.side_effect = lambda service, key: (
        "malformed" if key.endswith(":pending") else original(service, key)
    )
    with pytest.raises(NativeCredentialStoreError):
        store.save(ACTOR, {"token": CANARY}, "v2")
    assert store.load(active) == {"token": "old-synthetic"}


def test_rotation_and_stale_revoke(sandbox: tuple[Path, Path, Mock]) -> None:
    project, _, _ = sandbox
    store = NativeCredentialStore(project, cloud_mode=True)
    old = store.save(ACTOR, {"token": "old-synthetic"}, "v1")
    new = store.save(ACTOR, {"token": CANARY}, "v2")
    assert old.credential_id != new.credential_id
    assert store.load(old) is None
    store.revoke(old)
    assert store.load(new) == {"token": CANARY}


def test_revoke_prevents_load(sandbox: tuple[Path, Path, Mock]) -> None:
    project, _, backend = sandbox
    store = NativeCredentialStore(project, cloud_mode=False)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    backend.delete_password.side_effect = RuntimeError(CANARY)
    with pytest.raises(NativeCredentialStoreError):
        store.revoke(ref)
    backend.get_password.reset_mock()
    assert store.load(ref) is None
    assert NativeCredentialStore(project, cloud_mode=False).load(ref) is None
    store.revoke(ref)
    backend.get_password.assert_not_called()


def test_backend_outage_does_not_resurrect_fallback(sandbox: tuple[Path, Path, Mock]) -> None:
    project, _, backend = sandbox
    store = NativeCredentialStore(project, cloud_mode=False)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    backend.get_password.side_effect = RuntimeError(CANARY)
    with pytest.raises(NativeCredentialStoreError):
        store.load(ref)


def test_canary_absent_from_metadata_log_and_error(
    sandbox: tuple[Path, Path, Mock],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, root, backend = sandbox
    store = NativeCredentialStore(project, cloud_mode=False)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    backend.get_password.side_effect = RuntimeError(CANARY)
    with pytest.raises(NativeCredentialStoreError) as caught:
        store.load(ref)
    assert caught.value.code and caught.value.action
    assert caught.value.__suppress_context__
    assert CANARY not in str(caught.value)
    assert CANARY not in caplog.text + str(capsys.readouterr()) + ref.model_dump_json()
    assert all(CANARY not in path.read_text() for path in _files(root))


@pytest.mark.parametrize("value", [None, {}, {"token": None}, {"token": 1}, {1: "x"}, []])
def test_invalid_credential_has_safe_error(sandbox: tuple[Path, Path, Mock], value: object) -> None:
    project, _, backend = sandbox
    with pytest.raises(NativeCredentialStoreError):
        NativeCredentialStore(project, cloud_mode=False).save(ACTOR, value, "v1")
    assert not backend.mock_calls


@pytest.mark.parametrize("field", ["project_id", "actor_id", "credential_id", "version"])
@pytest.mark.parametrize("value", ["", " ", "../escape", "a/b", "a\\b", "bad\n", None, 1])
def test_strict_ref(field: str, value: object) -> None:
    payload = {
        "project_id": PROJECT,
        "actor_id": ACTOR,
        "credential_id": "opaque-id",
        "version": "v1",
    }
    payload[field] = value
    with pytest.raises(ValidationError):
        SecretRef(**payload)


def test_ref_frozen_extra_forbid_and_bypassed_instances(sandbox: tuple[Path, Path, Mock]) -> None:
    project, _, backend = sandbox
    payload = {
        "project_id": PROJECT,
        "actor_id": ACTOR,
        "credential_id": "opaque-id",
        "version": "v1",
    }
    with pytest.raises(ValidationError):
        SecretRef(**payload, token=CANARY)
    ref = SecretRef(**payload)
    with pytest.raises(ValidationError):
        ref.version = "v2"
    with pytest.raises(NativeCredentialStoreError):
        NativeCredentialStore(project, cloud_mode=False).load(
            ref.model_copy(update={"actor_id": "../escape"})
        )
    assert not backend.mock_calls


def test_symlink_and_traversal_refused(sandbox: tuple[Path, Path, Mock]) -> None:
    project, root, backend = sandbox
    target = root.parent / "unrelated"
    target.mkdir(parents=True)
    root.symlink_to(target, target_is_directory=True)
    store = NativeCredentialStore(project, cloud_mode=False)
    with pytest.raises(NativeCredentialStoreError):
        store.save(ACTOR, {"token": CANARY}, "v1")
    assert not list(target.iterdir())
    assert not backend.mock_calls


@pytest.mark.parametrize("target_name", ["record.json", "lock"])
def test_symlink_existing_state_refused(sandbox: tuple[Path, Path, Mock], target_name: str) -> None:
    project, root, _ = sandbox
    store = NativeCredentialStore(project, cloud_mode=True)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    actor_dir = next(root.glob("*/*"))
    unrelated = root.parent / "unrelated-file"
    unrelated.write_text("untouched")
    target = actor_dir / target_name
    target.unlink()
    target.symlink_to(unrelated)
    with pytest.raises(NativeCredentialStoreError):
        store.load(ref)
    assert unrelated.read_text() == "untouched"


def test_malformed_metadata_denied_before_secret_lookup(sandbox: tuple[Path, Path, Mock]) -> None:
    project, root, backend = sandbox
    store = NativeCredentialStore(project, cloud_mode=False)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    record = next(root.rglob("record.json"))
    data = json.loads(record.read_text())
    data["ref"]["version"] = "tampered"
    record.write_text(json.dumps(data))
    backend.get_password.reset_mock()
    assert store.load(ref) is None
    backend.get_password.assert_not_called()
    record.write_text(CANARY)
    with pytest.raises(NativeCredentialStoreError):
        store.load(ref)
    backend.get_password.assert_not_called()


def test_external_root_inside_project_refused(
    sandbox: tuple[Path, Path, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, backend = sandbox
    monkeypatch.setattr(credentials, "_LOCAL_SECRETS_DIR", project / "secrets/native")
    with pytest.raises(NativeCredentialStoreError):
        NativeCredentialStore(project, cloud_mode=False).save(ACTOR, {"token": CANARY}, "v1")
    assert not backend.mock_calls


def test_project_metadata_symlink_refused(sandbox: tuple[Path, Path, Mock]) -> None:
    project, root, backend = sandbox
    target = root.parent / "external-project.yml"
    target.parent.mkdir(parents=True)
    metadata = project / ".dango/project.yml"
    target.write_bytes(metadata.read_bytes())
    metadata.unlink()
    metadata.symlink_to(target)
    with pytest.raises(NativeCredentialStoreError):
        NativeCredentialStore(project, cloud_mode=False)
    assert not backend.mock_calls


def test_secret_symlink_refused(sandbox: tuple[Path, Path, Mock]) -> None:
    project, root, _ = sandbox
    store = NativeCredentialStore(project, cloud_mode=True)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    secret = next(path for path in root.rglob("*.json") if path.name != "record.json")
    target = root.parent / "external-secret"
    target.write_text('{"token":"external-synthetic"}')
    secret.unlink()
    secret.symlink_to(target)
    with pytest.raises(NativeCredentialStoreError):
        store.load(ref)
    assert target.read_text() == '{"token":"external-synthetic"}'


def test_two_actors_and_serialized_writers(sandbox: tuple[Path, Path, Mock]) -> None:
    from concurrent.futures import ThreadPoolExecutor

    project, _, _ = sandbox
    store = NativeCredentialStore(project, cloud_mode=True)
    other = store.save("other-actor", {"token": "other-synthetic"}, "v1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.save, ACTOR, {"token": str(i)}, str(i)) for i in range(2)]
        refs = [future.result() for future in futures]
    resolved = [store.load(ref) for ref in refs]
    assert sum(value is not None for value in resolved) == 1
    assert store.load(other) == {"token": "other-synthetic"}


@pytest.mark.parametrize("project_data", [None, {}, {"id": ""}, {"id": 1}, {"id": None}])
def test_missing_or_invalid_persisted_identity(
    sandbox: tuple[Path, Path, Mock], project_data: object
) -> None:
    project, _, backend = sandbox
    (project / ".dango/project.yml").write_text(yaml.safe_dump({"project": project_data}))
    with pytest.raises(NativeCredentialStoreError):
        NativeCredentialStore(project, cloud_mode=False)
    assert not backend.mock_calls


@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_unsafe_metadata_permissions_refused_before_lookup(
    sandbox: tuple[Path, Path, Mock], mode: int
) -> None:
    project, root, backend = sandbox
    store = NativeCredentialStore(project, cloud_mode=False)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    next(root.rglob("record.json")).chmod(mode)
    backend.get_password.reset_mock()
    with pytest.raises(NativeCredentialStoreError):
        store.load(ref)
    backend.get_password.assert_not_called()


@pytest.mark.parametrize("entry", ["lock", "record.json"])
def test_fifo_refused_without_blocking(sandbox: tuple[Path, Path, Mock], entry: str) -> None:
    import os

    project, root, _ = sandbox
    store = NativeCredentialStore(project, cloud_mode=True)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    actor_dir = next(root.glob("*/*"))
    target = actor_dir / entry
    target.unlink()
    os.mkfifo(target, 0o600)
    with pytest.raises(NativeCredentialStoreError):
        store.load(ref)


def test_missing_refs_have_no_secret_lookup(sandbox: tuple[Path, Path, Mock]) -> None:
    project, _, backend = sandbox
    ref = SecretRef(project_id=PROJECT, actor_id=ACTOR, credential_id="unknown", version="v1")
    store = NativeCredentialStore(project, cloud_mode=False)
    assert store.load(ref) is None
    store.revoke(ref)
    assert not backend.mock_calls


def test_cloud_refuses_keyring_record_before_lookup(sandbox: tuple[Path, Path, Mock]) -> None:
    project, root, backend = sandbox
    store = NativeCredentialStore(project, cloud_mode=True)
    ref = store.save(ACTOR, {"token": CANARY}, "v1")
    path = next(root.rglob("record.json"))
    record = json.loads(path.read_text())
    record["backend"] = "keyring"
    path.write_text(json.dumps(record))
    with pytest.raises(NativeCredentialStoreError):
        store.load(ref)
    with pytest.raises(NativeCredentialStoreError):
        store.revoke(ref)
    with pytest.raises(NativeCredentialStoreError):
        store.save(ACTOR, {"token": CANARY}, "v2")
    assert not backend.mock_calls
