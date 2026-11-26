"""tests/integration/workspace_observations.py

Keep independent lifecycle oracles and fixed affected selection separate from native setup.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch

AFFECTED = ("08", "12", "16")
RETAINED = {
    "head": "f01d88b059c5e5de1e774372f85a72697fa35c74",
    "run": "45673095810",
    "cases": {f"{n:02d}": "PASS" for n in range(1, 19)},
}
RETAINED_AFFECTED = {
    "head": "b2c181653ee371e553c24eaaabd0bce486009b97",
    "run": "45728157345",
    "cases": {
        f"{n:02d}": "PASS" if f"{n:02d}" in {"08", "09", "12", "14", "16", "17", "18"} else "UNRUN"
        for n in range(1, 19)
    },
}


def snapshot_identity(rig, workspace, context, expected, fields, expiry=None):
    """Refuse saved owner/reference contradictions without changing any journal or native state."""
    from native_workspaces import denied

    if expiry is None:
        path = next(
            p
            for p in (rig.native.root / ".dango/state/workspaces/operations").glob("*.json")
            if json.loads(p.read_text())["operation_id"] == context.operation_id
        )
    else:
        path, _ = rig.manifest(workspace.workspace_id)
    raw = path.read_bytes()

    def replay():
        if expiry is None:
            return rig.service.allocate(context, "feature/same", "fixture-commit", "remote")
        return rig.service.extend(context, workspace.workspace_id, 0, expiry)

    assert replay() == expected
    for field in fields:
        value = json.loads(raw)
        snapshot = (
            value["result"]
            if expiry is None
            else next(
                c["workspace"]
                for c in value["changes"]
                if c["operation_id"] == context.operation_id
            )
        )
        snapshot[field] = "other-owner-or-reference"
        path.write_text(json.dumps(value))
        manifests = {
            str(p): p.read_bytes()
            for p in (rig.native.root / ".dango/state/workspaces").rglob("*.json")
        }
        operations = rig.rows("SELECT * FROM platform_operations ORDER BY operation_id")
        effects = rig.rows("SELECT * FROM platform_effects ORDER BY operation_id,effect_id")
        with patch.object(rig.native.port, "_connect", wraps=rig.native.port._connect) as calls:
            assert denied(replay) == "workspace_unavailable"
            assert calls.call_count == 0
        assert manifests == {
            str(p): p.read_bytes()
            for p in (rig.native.root / ".dango/state/workspaces").rglob("*.json")
        }
        assert rig.rows("SELECT * FROM platform_operations ORDER BY operation_id") == operations
        assert rig.rows("SELECT * FROM platform_effects ORDER BY operation_id,effect_id") == effects
        path.write_bytes(raw)
    assert replay() == expected


def historical_replay(rig, owner, workspace, context, expiry, first):
    """Preserve original allocation/extension snapshots after later extension and explicit expiry."""
    snapshot_identity(
        rig,
        workspace,
        context,
        first,
        ("actor_id", "project_id", "catalog_binding_ref", "data_root_ref"),
        expiry,
    )
    later_expiry = expiry + timedelta(days=1)
    later = rig.admit(
        "workspace.extend",
        {
            "workspace_id": workspace.workspace_id,
            "expiry": later_expiry.isoformat(timespec="microseconds"),
        },
        1,
    )
    latest = rig.service.extend(later, workspace.workspace_id, 1, later_expiry)
    assert latest.revision == 2
    assert rig.service.allocate(owner, "feature/same", "fixture-commit", "remote") == workspace
    assert rig.service.extend(context, workspace.workspace_id, 0, expiry) == first
    rig.dispose(latest)
    with patch.object(rig.native.port, "_connect", wraps=rig.native.port._connect) as calls:
        assert rig.service.allocate(owner, "feature/same", "fixture-commit", "remote") == workspace
        assert rig.service.extend(context, workspace.workspace_id, 0, expiry) == first
        assert calls.call_count == 0


def removal_snapshot(rig, workspace, context):
    """Retain exact independent SQL and JSON lifecycle facts for the D1 recovery oracle."""
    _, value = rig.manifest(workspace.workspace_id)
    return {
        "operation": rig.rows(
            "SELECT state,status,resource_revision,result_ref FROM platform_operations WHERE operation_id=?",
            (context.operation_id,),
        ),
        "effect": rig.rows(
            "SELECT state,native_ref FROM platform_effects WHERE operation_id=?",
            (context.operation_id,),
        ),
        "workspace": {
            "phase": value["phase"],
            "state": value["workspace"]["state"],
            "revision": value["workspace"]["revision"],
        },
    }


def saved_state_retains(rig, owner, workspace, binding, damage):
    """Check existing retention/corruption rows against actual populated native resources."""
    from native_workspaces import denied, expiry_context

    assert rig.service.has_pins(owner, workspace.workspace_id) is False
    path, value = rig.manifest(workspace.workspace_id)
    raw = path.read_bytes()
    if damage == "pins":
        del value["pins"]
    elif damage == "removal-owner":
        value["removal_operation_id"] = "unknown"
    elif damage == "unknown-operation":
        value["operations"].append("unknown-operation")
    else:
        value["workspace"]["state"] = damage
    path.write_text(json.dumps(value))
    context = expiry_context(rig, workspace)
    with patch.object(rig.native.port, "_connect", wraps=rig.native.port._connect) as calls:

        def action():
            return rig.service.expire(context, workspace.workspace_id, 0)

        if damage == "building":
            assert action().error_code == "workspace_retained"
        elif damage == "unknown-operation":
            damaged = path.read_bytes()
            assert (
                denied(lambda: rig.service.has_pins(owner, workspace.workspace_id))
                == "workspace_denied"
            )
            assert path.read_bytes() == damaged
            assert denied(action) == "workspace_denied"
        else:
            assert denied(action) == "workspace_unavailable"
        assert calls.call_count == 0
    with rig.native.allocator().attach(owner, binding, read_only=True) as connection:
        assert connection.execute("SELECT sum(amount) FROM catalog.sales.orders").fetchone() == (
            350,
        )
    path.write_bytes(raw)


def blocked_waiter(rig):
    """Schedule two real expiry calls around a finalized blocked receipt and clock change."""
    _, workspace = rig.allocate()
    context = rig.admit("workspace.expire", {"workspace_id": workspace.workspace_id}, 0)
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
            before = removal_snapshot(rig, workspace, context)
            path, _ = rig.manifest(workspace.workspace_id)
            manifest_before = path.read_bytes()
            rig.now = datetime.fromisoformat(workspace.lease_expiry) + timedelta(seconds=1)
        finally:
            release.set()
        with patch.object(rig.native.port, "_connect", wraps=rig.native.port._connect) as calls:
            assert waiter.result(timeout=10) == first
            assert calls.call_count == 0
    assert removal_snapshot(rig, workspace, context) == before
    assert path.read_bytes() == manifest_before
    rig.dispose(workspace)


def unapplied_extension(rig, owner):
    """Refuse new extension at the actual ready-manifest/prepared-allocation boundary."""
    from native_workspaces import denied

    from dango.storage.workspaces import Workspace

    original = rig.service._effect

    def interrupt(context, name, workspace_id, state):
        if name == "workspace.allocate" and state == "applied":
            raise InterruptedError()
        return original(context, name, workspace_id, state)

    with patch.object(rig.service, "_effect", interrupt):
        denied(lambda: rig.service.allocate(owner, "feature/same", "fixture-commit", "remote"))
    workspace_id = rig.rows(
        "SELECT native_ref FROM platform_effects WHERE operation_id=?", (owner.operation_id,)
    )[0][0]
    path, value = rig.manifest(workspace_id)
    workspace = Workspace.model_validate_json(json.dumps(value["workspace"]))
    expiry = datetime.fromisoformat(workspace.lease_expiry) + timedelta(days=1)
    context = rig.admit(
        "workspace.extend",
        {"workspace_id": workspace_id, "expiry": expiry.isoformat(timespec="microseconds")},
        0,
    )
    before = removal_snapshot(rig, workspace, owner)
    manifest_before = path.read_bytes()
    assert before["effect"] == [("prepared", workspace_id)]
    with patch.object(rig.native.port, "_connect", wraps=rig.native.port._connect) as calls:
        assert denied(lambda: rig.service.get(owner, workspace_id)) == "workspace_recovery_required"
        assert (
            denied(lambda: rig.service.extend(context, workspace_id, 0, expiry))
            == "workspace_recovery_required"
        )
        assert calls.call_count == 0
    assert removal_snapshot(rig, workspace, owner) == before
    assert path.read_bytes() == manifest_before
    assert (
        rig.rows("SELECT state FROM platform_effects WHERE operation_id=?", (context.operation_id,))
        == []
    )
    rig.service.allocate(owner, "feature/same", "fixture-commit", "remote")
    extended = rig.service.extend(context, workspace_id, 0, expiry)
    assert extended.state == "ready" and extended.revision == 1
    assert rig.service.extend(context, workspace_id, 0, expiry) == extended
    return extended
