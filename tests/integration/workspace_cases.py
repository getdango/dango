"""tests/integration/workspace_cases.py

Exercise the finite workspace matrix against ordinary roles and independent journal/native oracles.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from native_workspaces import (
    child,
    denied,
    expiry_context,
    inspect_negatives,
    inspect_owned,
    pin_expiry_race,
    wait_child,
)
from workspace_observations import (
    blocked_waiter,
    removal_snapshot,
    saved_state_retains,
    unapplied_extension,
)

from dango.operations.contracts import OutcomeReceipt
from dango.storage.catalog_ownership import OwnedWorkspace
from dango.storage.workspace_state import LockedRecord
from dango.storage.workspaces import Workspace


def strict_record(rig):
    """01: validate exact record keys and hostile scalar/timestamp types on the target profile."""
    from pydantic import ValidationError

    _, workspace = rig.allocate()
    keys = "workspace_id project_id actor_id branch_ref code_commit execution_location catalog_binding_ref data_root_ref created_at lease_expiry state revision".split()
    assert set(workspace.model_dump()) == set(keys)
    for change in (
        {"revision": True},
        {"revision": float("nan")},
        {"extra": 1},
        {"created_at": "2026-01-01"},
        {"state": "unknown"},
    ):
        try:
            Workspace.model_validate(workspace.model_dump() | change)
        except ValidationError:
            continue
        raise AssertionError("Hostile record admitted")
    rig.dispose(workspace)
    return {"classification": "target_contract", "exact_fields": len(keys)}


def two_actors(rig):
    """04: actually select two ordinary role credentials on shared project roots."""
    peer = rig.peer()
    role = peer.native.sql(
        "SELECT current_user,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user"
    )
    assert role == [("t04b_writer_b", False, False, False, False, False)]
    left_context, left = rig.allocate()
    right_context, right = peer.allocate()
    a, b = rig.populate(left_context, left), peer.populate(right_context, right)
    assert len({left.workspace_id, right.workspace_id}) == 2
    assert a.data_root_ref != b.data_root_ref and a.metadata_schema != b.metadata_schema
    assert Path(a.data_root_ref).stat().st_ino != Path(b.data_root_ref).stat().st_ino
    left_meta, right_meta = rig.native.metadata(a), peer.native.metadata(b)
    assert left_meta["namespace"][0][0] != right_meta["namespace"][0][0]
    assert len({row[2] for row in left_meta["tables"] + right_meta["tables"]}) == 4
    denied(lambda: peer.service.get(right_context, left.workspace_id))
    rig.dispose(left)
    peer.dispose(right)
    return {"classification": "native", "ordinary_peer": role, "distinct_table_uuids": 4}


def distinct_and_replay(rig):
    """05: genuine new allocations differ; canonical admitted replay creates no second binding."""
    context, one = rig.allocate(key="distinct-replay")
    again, same = rig.allocate(key="distinct-replay")
    _, two = rig.allocate()
    assert context.operation_id == again.operation_id and one == same
    assert one.workspace_id != two.workspace_id
    assert one.catalog_binding_ref != two.catalog_binding_ref
    rig.dispose(one)
    rig.dispose(two)
    return {"classification": "native", "replay_id_reused": True, "new_id_distinct": True}


def admission_negatives(rig):
    """06: unadmitted/mismatched/finalized-without-effect inputs make no native connection."""
    context = rig.admit(
        "workspace.allocate",
        {"branch_ref": "feature/same", "code_commit": "fixture-commit", "location": "remote"},
    )
    unadmitted = context.model_copy(update={"operation_id": "unadmitted"})
    with patch.object(rig.native.port, "_connect", wraps=rig.native.port._connect) as calls:
        denied(lambda: rig.service.allocate(unadmitted, "feature/same", "fixture-commit", "remote"))
        denied(lambda: rig.service.allocate(context, "changed", "fixture-commit", "remote"))
        rig.finish(context)
        denied(lambda: rig.service.allocate(context, "feature/same", "fixture-commit", "remote"))
        assert calls.call_count == 0
    return {"classification": "native_boundary", "native_connections": 0}


def prepared_before_manifest(rig):
    """07: real child exit before filesystem binding reuses the raw journal's generated ID."""
    context = rig.admit(
        "workspace.allocate",
        {"branch_ref": "feature/same", "code_commit": "fixture-commit", "location": "remote"},
    )
    original = LockedRecord.save

    def interrupt(record, value):
        if value.kind == "workspace-v1":
            os._exit(73)
        return original(record, value)

    def action():
        with patch.object(LockedRecord, "save", interrupt):
            rig.service.allocate(context, "feature/same", "fixture-commit", "remote")

    child(action, 73)
    raw = rig.rows(
        "SELECT native_ref,state FROM platform_effects WHERE operation_id=?",
        (context.operation_id,),
    )
    assert len(raw) == 1 and raw[0][1] == "prepared"
    workspace = rig.service.allocate(context, "feature/same", "fixture-commit", "remote")
    assert workspace.workspace_id == raw[0][0]
    rig.dispose(workspace)
    return {"classification": "native_process_recovery", "prepared_id_reused": True}


def manifest_damage_and_terminal(rig):
    """08: late lost authority refuses native mutation; finalized terminal replay is immutable."""
    context = rig.admit(
        "workspace.allocate",
        {"branch_ref": "feature/same", "code_commit": "fixture-commit", "location": "remote"},
    )
    with patch.object(rig.store, "finalize_operation", side_effect=InterruptedError()):
        denied(lambda: rig.service.allocate(context, "feature/same", "fixture-commit", "remote"))
    workspace_id = rig.rows(
        "SELECT native_ref FROM platform_effects WHERE operation_id=?", (context.operation_id,)
    )[0][0]
    path, value = rig.manifest(workspace_id)
    raw = path.read_bytes()
    path.unlink()
    with patch.object(rig.native.port, "_connect", wraps=rig.native.port._connect) as calls:
        denied(lambda: rig.service.allocate(context, "feature/same", "fixture-commit", "remote"))
        assert calls.call_count == 0
    path.write_bytes(raw)
    path.chmod(0o600)
    workspace = rig.service.allocate(context, "feature/same", "fixture-commit", "remote")
    terminal_context = rig.admit("workspace.expire", {"workspace_id": workspace_id}, 0)
    receipt = rig.store.finalize_operation(
        terminal_context,
        OutcomeReceipt(
            operation_id=terminal_context.operation_id,
            status="recovery_required",
            result_ref=workspace_id,
            error_code="damaged_authority",
            action="Inspect retained evidence.",
        ),
    )
    before = removal_snapshot(rig, workspace, terminal_context)
    manifest_before = path.read_bytes()
    assert before["operation"] == [("finalized", "recovery_required", None, workspace_id)]
    assert before["effect"] == []
    assert before["workspace"] == {"phase": "ready", "state": "ready", "revision": 0}
    with patch.object(rig.native.port, "_connect", wraps=rig.native.port._connect) as calls:
        assert rig.service.expire(terminal_context, workspace_id, 0) == receipt
        assert calls.call_count == 0
    after = removal_snapshot(rig, workspace, terminal_context)
    assert after == before and path.read_bytes() == manifest_before
    rig.dispose(workspace)
    return {
        "classification": "native_boundary",
        "late_damage_denied": True,
        "terminal_replay_no_mutation": True,
        "terminal_raw_before": before,
        "terminal_raw_after": after,
    }


def partial_native_allocation(rig):
    """09: abrupt exit after real native preparation cannot expose ready before application."""
    context = rig.admit(
        "workspace.allocate",
        {"branch_ref": "feature/same", "code_commit": "fixture-commit", "location": "remote"},
    )
    original = OwnedWorkspace.checkpoint

    def interrupt(owned, native, phase="prepared"):
        original(owned, native, phase)
        if phase == "prepared":
            os._exit(73)

    def action():
        with patch.object(OwnedWorkspace, "checkpoint", interrupt):
            rig.service.allocate(context, "feature/same", "fixture-commit", "remote")

    child(action, 73)
    workspace_id = rig.rows(
        "SELECT native_ref FROM platform_effects WHERE operation_id=?", (context.operation_id,)
    )[0][0]
    binding = rig.native.current(workspace_id)
    denied(lambda: rig.service.get(context, workspace_id))
    assert rig.manifest(workspace_id)[1]["workspace"] is None
    workspace = unapplied_extension(rig, context)
    assert (
        workspace.workspace_id == workspace_id
        and workspace.catalog_binding_ref == binding.binding_id
    )
    rig.dispose(workspace)
    return {"classification": "native_process_recovery", "binding_reused": True}


def concurrent_allocation(rig):
    """10: two actual processes contend on one canonical operation/native allocation."""
    gate = rig.native.root / "concurrent-go"
    outputs = [rig.native.root / f"concurrent-{n}" for n in range(2)]

    def action(index):
        while not gate.exists():
            time.sleep(0.01)
        _, workspace = rig.allocate(key="concurrent-native")
        outputs[index].write_text(workspace.workspace_id)

    pids = [child(lambda n=n: action(n), wait=False) for n in range(2)]
    gate.touch(mode=0o600)
    for pid in pids:
        wait_child(pid)
    ids = [path.read_text() for path in outputs]
    assert len(set(ids)) == 1
    _, workspace = rig.allocate(key="concurrent-native")
    assert workspace.workspace_id == ids[0]
    rig.dispose(workspace)
    return {"classification": "native_process", "distinct_ids": len(set(ids))}


def wrong_owner(rig):
    """11: admitted foreign actors/projects cannot read, extend, expire or pin this workspace."""
    _, workspace = rig.allocate()
    raw = rig.manifest(workspace.workspace_id)[0].read_bytes()
    for change in ({"actor_id": "foreign"}, {"project_id": "foreign"}):
        context = rig.native.context.model_copy(
            update=change | {"operation_id": os.urandom(16).hex()}
        )
        from dango.operations.contracts import OperationRequest

        rig.store.begin_operation(context, OperationRequest(operation_name="query", payload={}))
        with patch.object(rig.native.port, "_connect", wraps=rig.native.port._connect) as calls:
            denied(lambda context=context: rig.service.get(context, workspace.workspace_id))
            denied(
                lambda context=context: rig.service.acquire_pin(
                    context, workspace.workspace_id, "query", "result"
                )
            )
            future = rig.now + timedelta(days=9)
            for name in ("workspace.extend", "workspace.expire"):
                mutation = context.model_copy(update={"operation_id": os.urandom(16).hex()})
                payload = {"workspace_id": workspace.workspace_id}
                if name == "workspace.extend":
                    payload["expiry"] = future.isoformat(timespec="microseconds")
                rig.store.begin_operation(
                    mutation,
                    OperationRequest(
                        operation_name=name,
                        payload=payload,
                        expected_revision="0",
                    ),
                )
                if name == "workspace.extend":
                    denied(
                        lambda mutation=mutation, future=future: rig.service.extend(
                            mutation, workspace.workspace_id, 0, future
                        )
                    )
                else:
                    denied(
                        lambda mutation=mutation: rig.service.expire(
                            mutation, workspace.workspace_id, 0
                        )
                    )
            assert calls.call_count == 0
    assert rig.manifest(workspace.workspace_id)[0].read_bytes() == raw
    rig.dispose(workspace)
    return {"classification": "native_boundary", "owner_dimensions_denied": 2}


def lease_cas(rig):
    """12: exact extension CAS/replay increments once and stale input never mutates."""
    _, workspace = rig.allocate()
    rig.now = datetime.fromisoformat(workspace.lease_expiry) - timedelta(minutes=5)
    assert (
        timedelta(0)
        < datetime.fromisoformat(workspace.lease_expiry) - rig.now
        < timedelta(minutes=10)
    )
    expiry = rig.now + timedelta(days=2)
    payload = {
        "workspace_id": workspace.workspace_id,
        "expiry": expiry.isoformat(timespec="microseconds"),
    }
    context = rig.admit("workspace.extend", payload, 0)
    current = rig.service.extend(context, workspace.workspace_id, 0, expiry)
    assert current.revision == 1
    assert rig.service.extend(context, workspace.workspace_id, 0, expiry) == current
    stale = rig.admit("workspace.extend", payload, 0)
    denied(lambda: rig.service.extend(stale, workspace.workspace_id, 0, expiry))
    assert rig.manifest(workspace.workspace_id)[1]["workspace"]["revision"] == 1
    rig.dispose(current)
    return {"classification": "native_join", "extension_revision": 1}


def pins(rig):
    """13: pin identity, finalized retention and owning idempotent release are independent of lease."""
    owner, workspace = rig.allocate()
    for purpose in ("input", "build", "query", "backup"):
        context = rig.admit("consumer", {})
        pin = rig.service.acquire_pin(context, workspace.workspace_id, purpose, "resource")
        assert rig.service.acquire_pin(context, workspace.workspace_id, purpose, "resource") == pin
        rig.finish(context)
        assert rig.service.has_pins(owner, workspace.workspace_id)
        denied(lambda pin=pin: rig.service.release_pin(owner, workspace.workspace_id, pin.pin_id))
        rig.service.release_pin(context, workspace.workspace_id, pin.pin_id)
        rig.service.release_pin(context, workspace.workspace_id, pin.pin_id)
    assert not rig.service.has_pins(owner, workspace.workspace_id)
    rig.dispose(workspace)
    return {"classification": "native_join", "purposes": 4}


def active_retention(rig):
    """14: lease, pin, then active released-pin operation each prevent actual populated removal."""
    blocked_waiter(rig)
    owner, workspace = rig.allocate()
    binding = rig.populate(owner, workspace)
    assert rig.expire(workspace).error_code == "workspace_lease_active"
    context = rig.admit("query", {})
    pin = rig.service.acquire_pin(context, workspace.workspace_id, "query", "result")
    expiry = expiry_context(rig, workspace)
    assert rig.service.expire(expiry, workspace.workspace_id, 0).error_code == "workspace_retained"
    rig.service.release_pin(context, workspace.workspace_id, pin.pin_id)
    assert rig.expire(workspace).error_code == "workspace_retained"
    with rig.native.allocator().attach(owner, binding, read_only=True) as connection:
        assert connection.execute("SELECT sum(amount) FROM catalog.sales.orders").fetchone() == (
            350,
        )
    rig.finish(context)
    saved_state_retains(rig, owner, workspace, binding, "building")
    rig.dispose(workspace)
    return {"classification": "native", "retained_sales_sum": 350, "building_retained": True}


def cross_process_pin(rig):
    """15: a pin acquired in another process blocks actual removal until owned release/finalization."""
    owner, workspace = rig.allocate()
    binding = rig.populate(owner, workspace)
    context = rig.admit("query", {})
    path = rig.native.root / "process-pin.json"

    def acquire():
        pin = rig.service.acquire_pin(context, workspace.workspace_id, "query", "result")
        path.write_text(pin.model_dump_json())
        path.chmod(0o600)

    child(acquire)
    pin_id = json.loads(path.read_text())["pin_id"]
    expiry = expiry_context(rig, workspace)
    assert rig.service.expire(expiry, workspace.workspace_id, 0).error_code == "workspace_retained"
    assert Path(binding.data_root_ref).exists()
    with rig.native.allocator().attach(owner, binding, read_only=True) as connection:
        assert connection.execute("SELECT sum(amount) FROM catalog.sales.orders").fetchone() == (
            350,
        )

    def release():
        rig.service.release_pin(context, workspace.workspace_id, pin_id)
        rig.finish(context)

    child(release)
    rig.dispose(workspace)
    return {"classification": "native_process", "pin_blocked_actual_removal": True}


def corrupt_pin(rig):
    """16: corrupt pins or contradictory known state preserve populated native resources."""
    owner, workspace = rig.allocate()
    binding = rig.populate(owner, workspace)
    for damage in ("pins", "expired", "removal-owner", "unknown-operation"):
        saved_state_retains(rig, owner, workspace, binding, damage)
    rig.dispose(workspace)
    return {"classification": "native_boundary", "preserved_sales_sum": 350}


def removal_recovery(rig):
    """17: caught and abrupt native removal interruption retain active intent and exact revisions."""
    evidence = {}
    for mode in ("caught", "abrupt"):
        owner, workspace = rig.allocate()
        binding = rig.populate(owner, workspace)
        context = expiry_context(rig, workspace)
        original = rig.native.port.remove_owned

        def interrupt(*args, original=original, mode=mode, **kwargs):
            original(*args, **kwargs)
            if mode == "abrupt":
                os._exit(73)
            raise InterruptedError("fixed native boundary")

        def action(context=context, workspace=workspace, interrupt=interrupt):
            with patch.object(rig.native.port, "remove_owned", interrupt):
                rig.service.expire(context, workspace.workspace_id, 0)

        if mode == "abrupt":
            child(action, 73)
        else:
            denied(action)
        before = removal_snapshot(rig, workspace, context)
        assert before["operation"] == [("prepared", None, None, None)]
        assert before["effect"] == [("prepared", workspace.workspace_id)]
        assert before["workspace"] == {
            "phase": "removing",
            "state": "recovery_required",
            "revision": 1,
        }
        assert not rig.native.sql(
            f"SELECT oid FROM pg_namespace WHERE nspname='{binding.metadata_schema}'"
        )
        sentinel = Path(binding.data_root_ref) / "foreign.sentinel"
        sentinel.write_text("preserve")
        denied(
            lambda context=context, workspace=workspace: rig.service.expire(
                context, workspace.workspace_id, 0
            )
        )
        assert sentinel.read_text() == "preserve"
        sentinel.unlink()
        receipt = rig.service.expire(context, workspace.workspace_id, 0)
        assert receipt.status == "success" and receipt.resource_revision == "2"
        after = removal_snapshot(rig, workspace, context)
        assert after["operation"] == [("finalized", "success", "2", workspace.workspace_id)]
        assert after["effect"] == [("applied", workspace.workspace_id)]
        assert after["workspace"] == {"phase": "expired", "state": "expired", "revision": 2}
        assert not Path(binding.data_root_ref).exists()
        evidence[mode] = {
            "raw_before": before,
            "raw_after": after,
            "foreign_preserved": True,
        }
    evidence["pin_expiry_race"] = pin_expiry_race(rig)
    return {"classification": "native_process_recovery", **evidence}


CASES = list(
    zip(
        (f"{n:02d}" for n in range(1, 18)),
        (
            strict_record,
            inspect_owned,
            inspect_negatives,
            two_actors,
            distinct_and_replay,
            admission_negatives,
            prepared_before_manifest,
            manifest_damage_and_terminal,
            partial_native_allocation,
            concurrent_allocation,
            wrong_owner,
            lease_cas,
            pins,
            active_retention,
            cross_process_pin,
            corrupt_pin,
            removal_recovery,
        ),
        strict=True,
    )
)
