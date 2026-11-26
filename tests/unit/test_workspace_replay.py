"""tests/unit/test_workspace_replay.py

Regress scoped reference validation and real concurrent/recovery readiness boundaries.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import pytest

from dango.storage.workspaces import WorkspaceError
from tests.unit.workspace_support import Rig

pytestmark = pytest.mark.unit


def test_has_pins_validates_non_pin_operation_references(tmp_path):
    rig = Rig(tmp_path)
    owner, workspace = rig.allocate()
    assert rig.service.has_pins(owner, workspace.workspace_id) is False
    activity = rig.admit("query", {})
    path, value = rig.manifest(workspace.workspace_id)
    value["operations"].append(activity.operation_id)
    path.write_text(json.dumps(value))
    assert rig.service.has_pins(owner, workspace.workspace_id) is False
    value["operations"].append("unknown-operation")
    path.write_text(json.dumps(value))
    before, calls = path.read_bytes(), list(rig.port.calls)
    with pytest.raises(WorkspaceError):
        rig.service.has_pins(owner, workspace.workspace_id)
    assert path.read_bytes() == before and rig.port.calls == calls


def test_expiry_waiter_replays_final_blocked_receipt_after_clock_changes(tmp_path):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    context = rig.expiry(workspace)
    entered, release = threading.Event(), threading.Event()
    original = rig.service._admit

    def admit(*args, **kwargs):
        result = original(*args, **kwargs)
        if threading.current_thread().name.startswith("waiter"):
            entered.set()
            assert release.wait(10)
        return result

    with (
        patch.object(rig.service, "_admit", admit),
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="waiter") as pool,
    ):
        waiter = pool.submit(rig.service.expire, context, workspace.workspace_id, 0)
        try:
            assert entered.wait(10)
            first = rig.service.expire(context, workspace.workspace_id, 0)
            assert first.status == "blocked" and first.error_code == "workspace_lease_active"
            path, _ = rig.manifest(workspace.workspace_id)
            before, calls = path.read_bytes(), list(rig.port.calls)
            rig.now += timedelta(days=8)
        finally:
            release.set()
        assert waiter.result(timeout=10) == first
    assert path.read_bytes() == before and rig.port.calls == calls
    assert rig.rows(
        "SELECT state,status FROM platform_operations WHERE operation_id=?", (context.operation_id,)
    ) == [("finalized", "blocked")]


def test_extension_cannot_expose_ready_before_allocation_effect_applies(tmp_path):
    rig = Rig(tmp_path)
    owner = rig.allocation()
    original = rig.service._effect

    def interrupt(context, name, workspace_id, state):
        if name == "workspace.allocate" and state == "applied":
            raise InterruptedError()
        return original(context, name, workspace_id, state)

    with patch.object(rig.service, "_effect", interrupt), pytest.raises(WorkspaceError):
        rig.service.allocate(owner, "feature/same", "commit", "remote")
    workspace_id = rig.rows(
        "SELECT native_ref FROM platform_effects WHERE operation_id=?", (owner.operation_id,)
    )[0][0]
    path, _ = rig.manifest(workspace_id)
    expiry = rig.now + timedelta(days=9)
    context = rig.admit(
        "workspace.extend",
        {"workspace_id": workspace_id, "expiry": expiry.isoformat(timespec="microseconds")},
        revision=0,
    )
    before, calls = path.read_bytes(), list(rig.port.calls)
    with pytest.raises(WorkspaceError, match="workspace_recovery_required"):
        rig.service.get(owner, workspace_id)
    with pytest.raises(WorkspaceError, match="workspace_recovery_required"):
        rig.service.extend(context, workspace_id, 0, expiry)
    assert path.read_bytes() == before and rig.port.calls == calls
    assert (
        rig.rows("SELECT state FROM platform_effects WHERE operation_id=?", (context.operation_id,))
        == []
    )
    workspace = rig.service.allocate(owner, "feature/same", "commit", "remote")
    extended = rig.service.extend(context, workspace_id, 0, expiry)
    assert extended.state == "ready" and extended.revision == workspace.revision + 1 == 1
    assert rig.service.extend(context, workspace_id, 0, expiry) == extended


def extension(rig, workspace, days):
    expiry = rig.now + timedelta(days=days)
    context = rig.admit(
        "workspace.extend",
        {
            "workspace_id": workspace.workspace_id,
            "expiry": expiry.isoformat(timespec="microseconds"),
        },
        revision=workspace.revision,
    )
    return (
        context,
        expiry,
        rig.service.extend(context, workspace.workspace_id, workspace.revision, expiry),
    )


@pytest.mark.parametrize("kind", ["allocation", "extension"])
@pytest.mark.parametrize(
    "field", ["actor_id", "project_id", "catalog_binding_ref", "data_root_ref"]
)
def test_saved_replay_snapshot_must_match_enclosing_identity(tmp_path, kind, field):
    rig = Rig(tmp_path)
    owner, workspace = rig.allocate()
    if kind == "allocation":
        path = next((rig.root / ".dango/state/workspaces/operations").glob("*.json"))
        value = json.loads(path.read_text())
        snapshot = value["result"]

        def replay():
            return rig.service.allocate(owner, "feature/same", "commit", "remote")
    else:
        context, expiry, _ = extension(rig, workspace, 9)
        path, value = rig.manifest(workspace.workspace_id)
        snapshot = value["changes"][0]["workspace"]

        def replay():
            return rig.service.extend(context, workspace.workspace_id, 0, expiry)

    snapshot[field] = "other-owner-or-reference"
    path.write_text(json.dumps(value))
    before = {
        str(p): p.read_bytes() for p in (rig.root / ".dango/state/workspaces").rglob("*.json")
    }
    operations = rig.rows("SELECT * FROM platform_operations ORDER BY operation_id")
    effects = rig.rows("SELECT * FROM platform_effects ORDER BY operation_id,effect_id")
    calls = list(rig.port.calls)
    with pytest.raises(WorkspaceError):
        replay()
    assert before == {
        str(p): p.read_bytes() for p in (rig.root / ".dango/state/workspaces").rglob("*.json")
    }
    assert rig.rows("SELECT * FROM platform_operations ORDER BY operation_id") == operations
    assert rig.rows("SELECT * FROM platform_effects ORDER BY operation_id,effect_id") == effects
    assert rig.port.calls == calls


def test_healthy_historical_results_replay_after_extension_and_expiry(tmp_path):
    rig = Rig(tmp_path)
    owner, original = rig.allocate()
    context, expiry, first = extension(rig, original, 9)
    _, _, latest = extension(rig, first, 10)
    assert rig.service.allocate(owner, "feature/same", "commit", "remote") == original
    assert rig.service.extend(context, original.workspace_id, 0, expiry) == first
    rig.now += timedelta(days=11)
    assert rig.service.expire(rig.expiry(latest), latest.workspace_id, 2).status == "success"
    calls = list(rig.port.calls)
    assert rig.service.allocate(owner, "feature/same", "commit", "remote") == original
    assert rig.service.extend(context, original.workspace_id, 0, expiry) == first
    assert rig.port.calls == calls
