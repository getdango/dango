"""tests/unit/test_workspaces.py

Check workspace admission, identity, lease, retention and recovery against independent records.
"""

import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest

from dango.operations.contracts import OutcomeReceipt
from dango.storage.workspace_state import LockedRecord
from dango.storage.workspaces import WorkspaceError
from tests.unit.workspace_support import Rig

pytestmark = pytest.mark.unit


def test_two_actors_same_branch_are_isolated(tmp_path):
    left = Rig(tmp_path)
    right = Rig(tmp_path, actor="other")
    _, a = left.allocate()
    _, b = right.allocate()
    assert a.workspace_id != b.workspace_id
    assert a.catalog_binding_ref != b.catalog_binding_ref
    assert a.data_root_ref != b.data_root_ref
    assert a.branch_ref == b.branch_ref == "feature/same"


def test_one_actor_two_allocations_are_distinct(tmp_path):
    rig = Rig(tmp_path)
    context, first = rig.allocate(key="same")
    canonical = rig.allocation(key="same")
    assert canonical.operation_id == context.operation_id
    assert rig.service.allocate(canonical, "feature/same", "commit", "remote") == first
    _, second = rig.allocate()
    assert first.workspace_id != second.workspace_id
    assert len(list((tmp_path / "data").iterdir())) == 2


@pytest.mark.parametrize("change", [{"actor_id": "foreign"}, {"project_id": "foreign"}])
def test_wrong_actor_or_project_denied(tmp_path, change):
    rig = Rig(tmp_path)
    context, workspace = rig.allocate()
    foreign = rig.admit("query", {}, context=rig.context(**change))
    before = rig.manifest(workspace.workspace_id)[0].read_bytes()
    with pytest.raises(WorkspaceError):
        rig.service.get(foreign, workspace.workspace_id)
    with pytest.raises(WorkspaceError):
        rig.service.acquire_pin(foreign, workspace.workspace_id, "query", "result")
    with pytest.raises(WorkspaceError):
        rig.service.release_pin(foreign, workspace.workspace_id, "a" * 32)
    assert before == rig.manifest(workspace.workspace_id)[0].read_bytes()
    assert rig.service.get(context, workspace.workspace_id) == workspace


def test_unadmitted_mismatched_and_finalized_without_effect_create_no_native(tmp_path):
    rig = Rig(tmp_path)
    unadmitted = rig.context()
    with pytest.raises(WorkspaceError):
        rig.service.allocate(unadmitted, "feature/same", "commit", "remote")
    admitted = rig.allocation()
    with pytest.raises(WorkspaceError):
        rig.service.allocate(admitted, "different", "commit", "remote")
    rig.finish(admitted)
    with pytest.raises(WorkspaceError):
        rig.service.allocate(admitted, "feature/same", "commit", "remote")
    assert rig.port.calls == []
    assert not list((tmp_path / "data").iterdir())


def test_lease_warning_extension_and_revision_conflict(tmp_path):
    rig = Rig(tmp_path)
    owner, workspace = rig.allocate()
    rig.now += timedelta(days=7, minutes=-5)
    expiry = rig.now + timedelta(days=2)
    assert (
        timedelta(0)
        < __import__("datetime").datetime.fromisoformat(workspace.lease_expiry) - rig.now
        < timedelta(minutes=10)
    )
    payload = {
        "workspace_id": workspace.workspace_id,
        "expiry": expiry.isoformat(timespec="microseconds"),
    }
    context = rig.admit("workspace.extend", payload, revision=0)
    extended = rig.service.extend(context, workspace.workspace_id, 0, expiry)
    assert extended.revision == 1
    assert rig.manifest(workspace.workspace_id)[1]["workspace"]["revision"] == 1
    assert rig.service.extend(context, workspace.workspace_id, 0, expiry) == extended
    stale = rig.admit("workspace.extend", payload, revision=0)
    with pytest.raises(WorkspaceError, match="workspace_conflict"):
        rig.service.extend(stale, workspace.workspace_id, 0, expiry)
    assert rig.service.get(owner, workspace.workspace_id).revision == 1


@pytest.mark.parametrize("purpose", ["input", "build", "query", "backup"])
def test_active_operation_or_pin_prevents_expiry(tmp_path, purpose):
    rig = Rig(tmp_path)
    owner, workspace = rig.allocate()
    user = rig.admit("consumer", {})
    pin = rig.service.acquire_pin(user, workspace.workspace_id, purpose, "resource")
    assert rig.service.acquire_pin(user, workspace.workspace_id, purpose, "resource") == pin
    other = rig.admit("consumer", {})
    with pytest.raises(WorkspaceError):
        rig.service.release_pin(other, workspace.workspace_id, pin.pin_id)
    rig.finish(other)
    rig.now += timedelta(days=8)
    expiry = rig.expiry(workspace)
    assert rig.service.expire(expiry, workspace.workspace_id, 0).error_code == "workspace_retained"
    rig.service.release_pin(user, workspace.workspace_id, pin.pin_id)
    rig.service.release_pin(user, workspace.workspace_id, pin.pin_id)
    assert not rig.service.has_pins(owner, workspace.workspace_id)
    expiry = rig.expiry(workspace)
    assert rig.service.expire(expiry, workspace.workspace_id, 0).error_code == "workspace_retained"
    rig.finish(user)
    expiry = rig.expiry(workspace)
    receipt = rig.service.expire(expiry, workspace.workspace_id, 0)
    assert receipt.status == "success" and receipt.resource_revision == "2"
    assert rig.service.expire(expiry, workspace.workspace_id, 0) == receipt
    assert not __import__("pathlib").Path(workspace.data_root_ref).exists()
    assert rig.service.get(owner, workspace.workspace_id).state == "expired"


def test_partial_allocation_is_not_ready(tmp_path):
    rig = Rig(tmp_path)
    context = rig.allocation()
    rig.port.fail = "prepare"
    with pytest.raises(WorkspaceError, match="workspace_recovery_required"):
        rig.service.allocate(context, "feature/same", "commit", "remote")
    saved = rig.rows("SELECT native_ref,state FROM platform_effects")[0]
    assert saved[1] == "prepared"
    with pytest.raises(WorkspaceError):
        rig.service.get(context, saved[0])
    rig.port.fail = None
    workspace = rig.service.allocate(context, "feature/same", "commit", "remote")
    assert workspace.workspace_id == saved[0]
    assert len(list((tmp_path / "data").iterdir())) == 1


def test_prepared_effect_before_workspace_manifest_resumes_same_id(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    context = rig.allocation()
    original = LockedRecord.save

    def interrupt(record, value):
        if value.kind == "workspace-v1":
            raise InterruptedError("checkpoint")
        return original(record, value)

    with monkeypatch.context() as patch:
        patch.setattr(LockedRecord, "save", interrupt)
        with pytest.raises(WorkspaceError):
            rig.service.allocate(context, "feature/same", "commit", "remote")
    recorded = rig.rows("SELECT native_ref FROM platform_effects")[0][0]
    assert rig.port.calls == []
    workspace = rig.service.allocate(context, "feature/same", "commit", "remote")
    assert workspace.workspace_id == recorded


def test_extend_crash_result_is_atomic_with_revision(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    expiry = rig.now + timedelta(days=9)
    context = rig.admit(
        "workspace.extend",
        {
            "workspace_id": workspace.workspace_id,
            "expiry": expiry.isoformat(timespec="microseconds"),
        },
        revision=0,
    )
    original = rig.store.finalize_operation
    with monkeypatch.context() as patch:
        patch.setattr(
            rig.store, "finalize_operation", lambda *args: (_ for _ in ()).throw(InterruptedError())
        )
        with pytest.raises(WorkspaceError):
            rig.service.extend(context, workspace.workspace_id, 0, expiry)
    raw = rig.manifest(workspace.workspace_id)[1]
    assert raw["workspace"]["revision"] == 1
    assert raw["changes"][0]["operation_id"] == context.operation_id
    assert rig.rows(
        "SELECT state FROM platform_operations WHERE operation_id=?", (context.operation_id,)
    ) == [("prepared",)]
    assert rig.service.extend(context, workspace.workspace_id, 0, expiry).revision == 1
    assert rig.manifest(workspace.workspace_id)[1]["workspace"]["revision"] == 1
    assert (
        original(
            context,
            rig.store.inspect_effect(context, context.operation_id, "workspace.extend").receipt,
        ).status
        == "success"
    )


def test_caught_removal_failure_keeps_operation_active_then_resumes(tmp_path):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    rig.now += timedelta(days=8)
    expiry = rig.expiry(workspace)
    rig.port.fail = "remove"
    with pytest.raises(WorkspaceError, match="workspace_recovery_required"):
        rig.service.expire(expiry, workspace.workspace_id, 0)
    assert rig.rows(
        "SELECT state,status FROM platform_operations WHERE operation_id=?", (expiry.operation_id,)
    ) == [("prepared", None)]
    assert rig.manifest(workspace.workspace_id)[1]["workspace"]["revision"] == 1
    rig.port.fail = None
    receipt = rig.service.expire(expiry, workspace.workspace_id, 0)
    assert receipt.resource_revision == "2"
    assert rig.manifest(workspace.workspace_id)[1]["workspace"]["revision"] == 2


def test_terminal_final_recovery_is_immutable_without_native_work(tmp_path):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    expiry = rig.expiry(workspace)
    terminal = rig.store.finalize_operation(
        expiry,
        OutcomeReceipt(
            operation_id=expiry.operation_id,
            status="recovery_required",
            result_ref=workspace.workspace_id,
            error_code="damaged",
            action="Inspect ownership.",
        ),
    )
    before = list(rig.port.calls)
    assert rig.service.expire(expiry, workspace.workspace_id, 0) == terminal
    assert rig.port.calls == before


def test_unknown_pin_state_refuses_removal(tmp_path):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    path, value = rig.manifest(workspace.workspace_id)
    del value["pins"]
    path.write_text(json.dumps(value))
    rig.now += timedelta(days=8)
    expiry = rig.expiry(workspace)
    before = list(rig.port.calls)
    with pytest.raises(WorkspaceError):
        rig.service.expire(expiry, workspace.workspace_id, 0)
    assert rig.port.calls == before


def test_active_lease_and_finalized_pin_still_retain(tmp_path):
    rig = Rig(tmp_path)
    owner, workspace = rig.allocate()
    expiry = rig.expiry(workspace)
    assert (
        rig.service.expire(expiry, workspace.workspace_id, 0).error_code == "workspace_lease_active"
    )
    consumer = rig.admit("query", {})
    pin = rig.service.acquire_pin(consumer, workspace.workspace_id, "query", "result")
    rig.finish(consumer)
    rig.now += timedelta(days=8)
    expiry = rig.expiry(workspace)
    assert rig.service.expire(expiry, workspace.workspace_id, 0).error_code == "workspace_retained"
    assert rig.service.has_pins(owner, workspace.workspace_id)
    rig.service.release_pin(consumer, workspace.workspace_id, pin.pin_id)
    assert not rig.service.has_pins(owner, workspace.workspace_id)
    with pytest.raises(WorkspaceError):
        rig.service.acquire_pin(consumer, workspace.workspace_id, "query", "result")


def test_concurrent_process_admission_reuses_one_generated_workspace(tmp_path):
    rig = Rig(tmp_path)
    code = """
import sys
from tests.unit.workspace_support import Rig
rig=Rig(sys.argv[1])
print('ready',flush=True)
sys.stdin.readline()
context=rig.allocation(key='concurrent')
print(rig.service.allocate(context,'feature/same','commit','remote').workspace_id,flush=True)
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        for _ in range(2)
    ]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        ids = [child.communicate(timeout=30)[0].strip() for child in children]
        assert children[0].returncode == children[1].returncode == 0
        assert len(set(ids)) == 1
        assert rig.rows("SELECT native_ref,state FROM platform_effects") == [(ids[0], "applied")]
        assert len(list((tmp_path / "data").iterdir())) == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def test_abrupt_process_removal_preserves_active_intent_and_exact_revision(tmp_path):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    rig.now += timedelta(days=8)
    context = rig.expiry(workspace)
    code = """
import os,sys
from datetime import timedelta
from dango.operations.contracts import OperationContext
from tests.unit.workspace_support import Rig
rig=Rig(sys.argv[1]); rig.now+=timedelta(days=8)
context=OperationContext.model_validate_json(sys.argv[2])
original=rig.port.remove_owned
def interrupt(*args,**kwargs):
 original(*args,**kwargs)
 os._exit(73)
rig.port.remove_owned=interrupt
rig.service.expire(context,sys.argv[3],0)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(tmp_path),
            context.model_dump_json(),
            workspace.workspace_id,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 73
    assert rig.rows(
        "SELECT state,status FROM platform_operations WHERE operation_id=?", (context.operation_id,)
    ) == [("prepared", None)]
    assert rig.rows(
        "SELECT state,native_ref FROM platform_effects WHERE operation_id=?",
        (context.operation_id,),
    ) == [("prepared", workspace.workspace_id)]
    assert rig.manifest(workspace.workspace_id)[1]["workspace"]["revision"] == 1
    receipt = rig.service.expire(context, workspace.workspace_id, 0)
    assert receipt.status == "success" and receipt.resource_revision == "2"
    assert rig.manifest(workspace.workspace_id)[1]["workspace"]["revision"] == 2


def test_removed_tombstone_retry_still_requires_allocator_authorization(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    rig.now += timedelta(days=8)
    context = rig.expiry(workspace)
    with monkeypatch.context() as patch:
        patch.setattr(
            rig.store, "finalize_operation", lambda *args: (_ for _ in ()).throw(InterruptedError())
        )
        with pytest.raises(WorkspaceError):
            rig.service.expire(context, workspace.workspace_id, 0)
    before = rig.manifest(workspace.workspace_id)[0].read_bytes()
    rig.grant = False
    with pytest.raises(WorkspaceError):
        rig.service.expire(context, workspace.workspace_id, 0)
    assert rig.manifest(workspace.workspace_id)[0].read_bytes() == before
    assert rig.rows(
        "SELECT state FROM platform_operations WHERE operation_id=?", (context.operation_id,)
    ) == [("prepared",)]
    rig.grant = True
    assert rig.service.expire(context, workspace.workspace_id, 0).resource_revision == "2"


@pytest.mark.parametrize("revision", [True, -1, 1.0, "0"])
def test_invalid_revision_never_mutates(tmp_path, revision):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    operation = rig.expiry(workspace)
    before = rig.manifest(workspace.workspace_id)[0].read_bytes()
    with pytest.raises(WorkspaceError, match="invalid_input"):
        rig.service.expire(operation, workspace.workspace_id, revision)
    assert rig.manifest(workspace.workspace_id)[0].read_bytes() == before


@pytest.mark.parametrize("kind", ["shorter", "equal", "naive", "offset"])
def test_invalid_extension_expiry_never_mutates(tmp_path, kind):
    from datetime import timezone

    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    expiry = rig.now + timedelta(days=7 if kind == "equal" else 6)
    if kind == "naive":
        expiry = expiry.replace(tzinfo=None)
    elif kind == "offset":
        expiry = expiry.astimezone(timezone(timedelta(hours=1)))
    operation = rig.admit(
        "workspace.extend",
        {
            "workspace_id": workspace.workspace_id,
            "expiry": expiry.isoformat(timespec="microseconds"),
        },
        revision=0,
    )
    before = rig.manifest(workspace.workspace_id)[0].read_bytes()
    with pytest.raises(WorkspaceError, match="invalid_input"):
        rig.service.extend(operation, workspace.workspace_id, 0, expiry)
    assert rig.manifest(workspace.workspace_id)[0].read_bytes() == before


def test_waiting_retry_observes_finalization_inside_operation_lock(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    expiry = rig.now + timedelta(days=9)
    operation = rig.admit(
        "workspace.extend",
        {
            "workspace_id": workspace.workspace_id,
            "expiry": expiry.isoformat(timespec="microseconds"),
        },
        revision=0,
    )
    result = rig.service.extend(operation, workspace.workspace_id, 0, expiry)
    # Capture the already admitted snapshot before a second caller enters its operation lock.
    inspection = rig.store.inspect_effect(operation, operation.operation_id, "workspace.extend")
    active_snapshot = inspection.model_copy(update={"state": "prepared", "receipt": None})
    original = rig.service._admit

    def stale(*args, **kwargs):
        _, request = original(*args, **kwargs)
        return active_snapshot, request

    monkeypatch.setattr(rig.service, "_admit", stale)
    assert rig.service.extend(operation, workspace.workspace_id, 0, expiry) == result
    assert rig.manifest(workspace.workspace_id)[1]["workspace"]["revision"] == 1


def test_unresolved_terminal_recovery_retains_even_after_pin_release(tmp_path):
    rig = Rig(tmp_path)
    _, workspace = rig.allocate()
    consumer = rig.admit("query", {})
    pin = rig.service.acquire_pin(consumer, workspace.workspace_id, "query", "result")
    rig.service.release_pin(consumer, workspace.workspace_id, pin.pin_id)
    rig.store.finalize_operation(
        consumer,
        OutcomeReceipt(
            operation_id=consumer.operation_id,
            status="recovery_required",
            error_code="unknown_native_stop",
            action="Inspect native activity.",
        ),
    )
    rig.now += timedelta(days=8)
    expiry = rig.expiry(workspace)
    assert rig.service.expire(expiry, workspace.workspace_id, 0).error_code == "workspace_retained"
