"""tests/unit/test_workspace_state.py

Verify strict manifests, no-follow IO and independent process-lock behavior.
"""

import json
import os
import subprocess
import sys
from datetime import timedelta
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from dango.storage.workspace_records import OperationIntent, Workspace
from dango.storage.workspace_state import locked
from dango.storage.workspaces import WorkspaceError
from tests.unit.workspace_support import Rig

pytestmark = pytest.mark.unit


def test_workspace_exact_fields_and_strict_validation(tmp_path):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    fields = {
        "workspace_id",
        "project_id",
        "actor_id",
        "branch_ref",
        "code_commit",
        "execution_location",
        "catalog_binding_ref",
        "data_root_ref",
        "created_at",
        "lease_expiry",
        "state",
        "revision",
    }
    assert set(workspace.model_dump()) == fields
    for change in (
        {"revision": True},
        {"revision": -1},
        {"revision": 1.0},
        {"revision": float("nan")},
        {"state": "unknown"},
        {"execution_location": "automatic"},
        {"created_at": "2026-01-01"},
        {"lease_expiry": "2026-01-01T00:00:00+01:00"},
        {"actor_id": " "},
        {"extra": 1},
    ):
        with pytest.raises(ValidationError):
            Workspace.model_validate(workspace.model_dump() | change)
    for field in fields:
        value = workspace.model_dump()
        del value[field]
        with pytest.raises(ValidationError):
            Workspace.model_validate(value)


@pytest.mark.parametrize(
    "damage",
    ["missing-intent", "corrupt-intent", "late-missing-workspace", "unknown-pin-operation"],
)
def test_lost_or_unknown_authority_does_not_reconstruct_native_state(tmp_path, damage):
    rig = Rig(tmp_path)
    if damage == "late-missing-workspace":
        context = rig.allocation()
        with patch.object(rig.store, "finalize_operation", side_effect=InterruptedError()):
            with pytest.raises(WorkspaceError):
                rig.service.allocate(context, "feature/same", "commit", "remote")
        path = next((tmp_path / ".dango/state/workspaces/resources").glob("*.json"))
        workspace = Workspace.model_validate_json(
            json.dumps(json.loads(path.read_text())["workspace"])
        )
    else:
        context, workspace = rig.allocate()
    path, value = rig.manifest(workspace.workspace_id)
    intent = next((tmp_path / ".dango/state/workspaces/operations").glob("*.json"))
    if damage == "missing-intent":
        intent.unlink()
    elif damage == "corrupt-intent":
        intent.write_text('{"phase":')
    elif damage == "late-missing-workspace":
        path.unlink()
    else:
        value["operations"].append("unknown")
        path.write_text(json.dumps(value))
    before = list(rig.port.calls)
    if damage == "unknown-pin-operation":
        rig.now += timedelta(days=8)
        operation = rig.expiry(workspace)
        with pytest.raises(WorkspaceError):
            rig.service.expire(operation, workspace.workspace_id, 0)
    else:
        with pytest.raises(WorkspaceError):
            rig.service.allocate(context, "feature/same", "commit", "remote")
    assert rig.port.calls == before


def test_no_follow_and_private_terminal_modes(tmp_path):
    rig = Rig(tmp_path)
    context, workspace = rig.allocate()
    path, _ = rig.manifest(workspace.workspace_id)
    original = path.read_bytes()
    sentinel = tmp_path / "foreign"
    sentinel.write_bytes(original)
    path.unlink()
    path.symlink_to(sentinel)
    with pytest.raises(WorkspaceError):
        rig.service.get(context, workspace.workspace_id)
    assert sentinel.read_bytes() == original
    path.unlink()
    path.write_bytes(original)
    path.chmod(0o644)
    with pytest.raises(WorkspaceError):
        rig.service.get(context, workspace.workspace_id)
    path.chmod(0o600)
    os.link(path, tmp_path / "hardlink")
    with pytest.raises(WorkspaceError):
        rig.service.get(context, workspace.workspace_id)


def test_ancestor_symlink_and_permissive_state_root_refused(tmp_path):
    root = tmp_path / "actual"
    root.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        with locked(alias, "operations", ("project", "operation")):
            pass
    with locked(root, "operations", ("project", "operation")):
        pass
    (root / ".dango/state/workspaces").chmod(0o755)
    with pytest.raises(WorkspaceError):
        with locked(root, "operations", ("project", "operation")):
            pass


def test_process_lock_is_persistent_and_released_on_death(tmp_path):
    code = """
import sys
from pathlib import Path
from dango.storage.workspace_state import locked
with locked(Path(sys.argv[1]),'operations',('project','operation')):
 print('locked',flush=True)
 sys.stdin.readline()
"""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        lock = next((tmp_path / ".dango/state/workspaces/operations").glob("*.lock"))
        inode = lock.stat().st_ino
        child.kill()
        child.wait(timeout=10)
        with locked(tmp_path, "operations", ("project", "operation")):
            assert lock.stat().st_ino == inode
    finally:
        child.kill() if child.poll() is None else None
        child.wait(timeout=10)


def test_intent_snapshots_are_strict_and_import_has_no_effects(tmp_path):
    with locked(tmp_path, "operations", ("project", "operation")) as record:
        value = OperationIntent(
            operation_id="operation",
            project_id="project",
            actor_id="actor",
            name="workspace.allocate",
            request_digest="a" * 64,
            parameters_json="{}",
            phase="intent",
            workspace_id=None,
        )
        record.save(value)
        assert record.read(OperationIntent) == value
    code = """
from unittest.mock import patch
with patch('sqlite3.connect',side_effect=AssertionError('SQLite import IO')), patch('socket.socket',side_effect=AssertionError('network import IO')):
 import dango.storage.workspaces
print('safe')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.stdout.strip() == "safe"


@pytest.mark.parametrize("damage", ["duplicate", "missing-kind"])
def test_manifest_keys_cannot_hide_unknown_pin_state(tmp_path, damage):
    rig = Rig(tmp_path)
    context, workspace = rig.allocate()
    path, value = rig.manifest(workspace.workspace_id)
    if damage == "duplicate":
        path.write_text(path.read_text()[:-1] + ',"pins":[]}')
    else:
        del value["kind"]
        path.write_text(json.dumps(value))
    with pytest.raises(WorkspaceError):
        rig.service.has_pins(context, workspace.workspace_id)


def test_native_matrix_collection_has_no_service_or_credential_io():
    from pathlib import Path

    code = """
import runpy,sys
from pathlib import Path
from unittest.mock import patch
import duckdb
root=Path(sys.argv[1]); sys.path.insert(0,str(root/'tests/integration'))
original=Path.read_text
def read(path,*args,**kwargs):
 if str(path).startswith('/secrets'):
  raise AssertionError('collection credential IO')
 return original(path,*args,**kwargs)
with patch('sqlite3.connect',side_effect=AssertionError('collection SQLite IO')), patch('socket.socket',side_effect=AssertionError('collection network IO')), patch('duckdb.connect',side_effect=AssertionError('collection native IO')), patch.object(Path,'read_text',read):
 runpy.run_path(str(root/'tests/integration/native_workspaces.py'),run_name='collection_only')
 runpy.run_path(str(root/'tests/integration/workspace_cases.py'),run_name='collection_only')
print('safe')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(Path(__file__).parents[2])],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.stdout.strip() == "safe"


@pytest.mark.parametrize("damage", ["phase", "state", "revision", "removal-owner"])
def test_contradictory_known_manifest_state_refuses_removal(tmp_path, damage):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    path, value = rig.manifest(workspace.workspace_id)
    if damage == "phase":
        value["phase"] = "allocating"
    elif damage == "state":
        value["workspace"]["state"] = "expired"
    elif damage == "revision":
        value["workspace"]["revision"] = 4
    else:
        value["removal_operation_id"] = "unknown"
    path.write_text(json.dumps(value))
    rig.now += timedelta(days=8)
    context = rig.expiry(workspace)
    before = list(rig.port.calls)
    with pytest.raises(WorkspaceError, match="workspace_unavailable"):
        rig.service.expire(context, workspace.workspace_id, 0)
    assert rig.port.calls == before


def test_building_workspace_retains_without_a_consumer_reference(tmp_path):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    path, value = rig.manifest(workspace.workspace_id)
    value["workspace"]["state"] = "building"
    path.write_text(json.dumps(value))
    rig.now += timedelta(days=8)
    context = rig.expiry(workspace)
    before = list(rig.port.calls)
    receipt = rig.service.expire(context, workspace.workspace_id, 0)
    assert receipt.status == "blocked" and receipt.error_code == "workspace_retained"
    assert rig.port.calls == before
