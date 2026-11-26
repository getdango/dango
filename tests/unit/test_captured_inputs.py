"""tests/unit/test_captured_inputs.py

Check strict capture contracts and failure ownership locally; native answers are separate.
"""

import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from dango.storage.input_state import InputCaptureError
from dango.storage.inputs import CapturedRelation, InputSet
from tests.unit.input_support import InputRig

pytestmark = pytest.mark.unit


@pytest.fixture
def rig(tmp_path):
    value = InputRig(tmp_path)
    try:
        yield value
    finally:
        value.close()


def test_capture_350_remains_350_after_prod_360(rig):
    # Local metadata identity only: the literal native 350/360 oracle lives in native_inputs.
    context, captured = rig.capture()
    assert captured.state == "ready" and captured.catalog_snapshot_ids == {
        "accepted-production": "2"
    }
    rig.copy.actual = 360
    assert rig.capture(context)[1] == captured
    assert rig.copy.calls.count("copy") == 1


def test_baseline_and_candidate_share_input_identity(rig):
    context, captured = rig.capture()
    baseline = rig.capture_service.inspect_capture(context, captured.input_set_id)
    candidate = rig.capture_service.inspect_capture(context, captured.input_set_id)
    assert baseline == candidate == captured
    baseline.catalog_snapshot_ids["accepted-production"] = "foreign"
    assert rig.capture_service.inspect_capture(context, captured.input_set_id) == captured


def test_unselected_relation_absent(rig):
    _, captured = rig.capture()
    assert tuple(r.relation_id for r in captured.selected_upstream_relations) == ("sales",)
    assert captured.source_version_refs == ("source-v1-sales",)
    assert captured.checkpoint_refs == ("checkpoint-v1-sales",)
    assert rig.copy.calls.count("copy") == 1


def test_duplicate_table_names_in_separate_schemas(rig):
    _, captured = rig.capture(relations=("sales", "finance"))
    assert tuple((r.schema_name, r.table_name) for r in captured.selected_upstream_relations) == (
        ("sales", "orders"),
        ("finance", "orders"),
    )
    assert captured.captured_bytes == 180


def test_quota_denied_before_copy(rig):
    context = rig.capture_context(maximum=99)
    with pytest.raises(InputCaptureError, match="capture_quota_exceeded"):
        rig.capture(context, maximum=99)
    assert "copy" not in rig.copy.calls
    identity = rig.rows(
        "SELECT native_ref FROM platform_effects WHERE operation_id=?", (context.operation_id,)
    )[0][0]
    raw = rig.record(identity)[1]
    assert raw["phase"] == "failed" and raw["estimated_bytes"] == 100
    assert len(raw["pins"]) == 2
    assert rig.capture_service.inspect_capture(context, identity).state == "failed"
    with pytest.raises(InputCaptureError, match="capture_quota_exceeded"):
        rig.capture(context, maximum=99)
    assert rig.copy.calls.count("export") == 1


def test_copy_overrun_or_interrupt_never_marks_ready(rig):
    rig.copy.actual = 1001
    context = rig.capture_context()
    with pytest.raises(InputCaptureError, match="capture_quota_exceeded"):
        rig.capture(context)
    identity = rig.rows(
        "SELECT native_ref FROM platform_effects WHERE operation_id=?", (context.operation_id,)
    )[0][0]
    raw = rig.record(identity)[1]
    assert raw["phase"] == "failed" and raw["captured_bytes"] == 1001
    assert json.loads(raw["result_json"])["state"] == "failed"
    assert rig.rows(
        "SELECT state FROM platform_effects WHERE operation_id=?", (context.operation_id,)
    ) == [("prepared",)]


@pytest.mark.parametrize(
    "factor,code",
    [("snapshot", "capture_source_unavailable"), ("schema", "capture_schema_changed")],
)
def test_expired_snapshot_and_changed_schema_are_actionable(rig, factor, code):
    rig.copy.fail = factor
    with pytest.raises(InputCaptureError, match=code):
        rig.capture()
    assert "copy" not in rig.copy.calls


@pytest.mark.parametrize(
    "change",
    [
        {"bytes": True},
        {"bytes": -1},
        {"bytes": 1.1},
        {"bytes": float("nan")},
        {"relation_id": " "},
        {"extra": 1},
    ],
)
def test_captured_relation_strict(change):
    data = {
        "relation_id": "sales",
        "schema_name": "sales",
        "table_name": "orders",
        "schema_fingerprint": "fingerprint",
        "catalog_snapshot_id": "2",
        "captured_relation_ref": "opaque",
        "bytes": 0,
    }
    with pytest.raises(ValidationError):
        CapturedRelation(**(data | change))


@pytest.mark.parametrize(
    "change",
    [
        {"created_at": "2026-01-01"},
        {"estimated_bytes": True},
        {"checkpoint_refs": ["ref"]},
        {"catalog_snapshot_ids": {"": "2"}},
        {"state": "released"},
        {"extra": 1},
    ],
)
def test_input_set_strict(rig, change):
    _, captured = rig.capture()
    with pytest.raises(ValidationError):
        InputSet.model_validate(captured.model_dump() | change)


def test_unadmitted_losing_id_wrong_owner_and_duplicate_ids_no_copy(rig):
    calls = list(rig.copy.calls)
    for context in (rig.context(), rig.capture_context(context=rig.context(actor_id="other"))):
        with pytest.raises(InputCaptureError):
            rig.capture(context)
    context = rig.capture_context(key="canonical")
    losing = rig.context()
    canonical = rig.capture_context(key="canonical", context=losing)
    assert canonical.operation_id == context.operation_id != losing.operation_id
    with pytest.raises(InputCaptureError):
        rig.capture(losing)
    with pytest.raises(InputCaptureError):
        rig.capture(relations=("sales", "sales"))
    assert rig.copy.calls == calls


def test_lost_lifetime_refuses_inspect_but_original_release_works(rig):
    context, captured = rig.capture()
    import fcntl

    fcntl.flock(rig.guard, fcntl.LOCK_UN)
    with pytest.raises(InputCaptureError, match="capture_source_unavailable"):
        rig.capture_service.inspect_capture(context, captured.input_set_id)
    rig.capture_service.release_capture(context, captured.input_set_id)
    rig.capture_service.release_capture(context, captured.input_set_id)
    assert rig.capture(context)[1] == captured
    assert not rig.service.has_pins(context, rig.target.workspace_id)


def test_release_preserves_consumer_and_original_context(rig, monkeypatch):
    context, captured = rig.capture()
    consumer = rig.admit("reader", {})
    pin = rig.service.acquire_pin(consumer, rig.target.workspace_id, "query", captured.input_set_id)
    before = rig.record(captured.input_set_id)[0].read_bytes()
    with pytest.raises(InputCaptureError):
        rig.capture_service.release_capture(consumer, captured.input_set_id)
    assert rig.record(captured.input_set_id)[0].read_bytes() == before
    original = rig.service.release_pin
    calls = []

    def partial(ctx, workspace, identity):
        calls.append(identity)
        if len(calls) == 2:
            raise InterruptedError()
        original(ctx, workspace, identity)

    with monkeypatch.context() as patch:
        patch.setattr(rig.service, "release_pin", partial)
        with pytest.raises(InputCaptureError):
            rig.capture_service.release_capture(context, captured.input_set_id)
    rig.capture_service.release_capture(context, captured.input_set_id)
    assert rig.service.has_pins(context, rig.target.workspace_id)
    rig.now += timedelta(days=8)
    assert (
        rig.service.expire(rig.expiry(rig.target), rig.target.workspace_id, 0).error_code
        == "workspace_retained"
    )
    rig.service.release_pin(consumer, rig.target.workspace_id, pin.pin_id)
    rig.finish(consumer)
    assert (
        rig.service.expire(rig.expiry(rig.target), rig.target.workspace_id, 0).status == "success"
    )


def test_interrupted_copy_stays_prepared_and_never_recopies(rig):
    rig.copy.fail = "interrupt"
    context = rig.capture_context()
    with pytest.raises(KeyboardInterrupt):
        rig.capture(context)
    identity = rig.rows(
        "SELECT native_ref FROM platform_effects WHERE operation_id=?", (context.operation_id,)
    )[0][0]
    assert rig.record(identity)[1]["phase"] == "copying"
    assert rig.rows(
        "SELECT state FROM platform_operations WHERE operation_id=?", (context.operation_id,)
    ) == [("prepared",)]
    rig.copy.fail = None
    with pytest.raises(InputCaptureError, match="capture_recovery_required"):
        rig.capture(context)
    assert rig.copy.calls.count("copy") == 1
    assert rig.record(identity)[1]["phase"] == "failed"


def test_ready_before_journal_completion_recovers_without_copy(rig, monkeypatch):
    context = rig.capture_context()
    original = rig.store.finalize_operation
    with monkeypatch.context() as patch:
        patch.setattr(
            rig.store, "finalize_operation", lambda *a: (_ for _ in ()).throw(InterruptedError())
        )
        with pytest.raises(InputCaptureError, match="capture_recovery_required"):
            rig.capture(context)
    identity = rig.rows(
        "SELECT native_ref FROM platform_effects WHERE operation_id=?", (context.operation_id,)
    )[0][0]
    assert rig.record(identity)[1]["phase"] == "ready"
    with pytest.raises(InputCaptureError, match="capture_recovery_required"):
        rig.capture_service.inspect_capture(context, identity)
    assert rig.capture(context)[1].input_set_id == identity
    assert rig.copy.calls.count("copy") == 1
    assert (
        original(
            context,
            rig.store.inspect_effect(context, context.operation_id, "inputs.capture").receipt,
        ).status
        == "success"
    )


@pytest.mark.parametrize(
    "code",
    [
        "capture_quota_exceeded",
        "capture_schema_changed",
        "capture_collision",
        "capture_source_unavailable",
    ],
)
def test_domain_refusal_survives_real_allocator_yield_boundary(rig, code):
    """The actual allocator wraps yield exceptions; capture must carry its safe code past closure."""
    from dango.storage.catalogs import CatalogError
    from dango.storage.input_copy import NativeInputCopy
    from dango.storage.workspace_state import current_binding

    binding, _ = current_binding(rig.allocator, rig.source_context, rig.source.workspace_id)
    with pytest.raises(CatalogError):
        with rig.allocator.attach(rig.source_context, binding, read_only=True):
            raise InputCaptureError(code)
    copy = NativeInputCopy(rig.root, rig.allocator)
    with pytest.raises(InputCaptureError) as caught:
        with copy._attach(rig.source_context, binding, read_only=True):
            raise InputCaptureError(code)
    assert caught.value.code == code
    assert rig.port.calls[-1] == "attach"


def test_fresh_store_and_services_reopen_saved_capture(rig):
    """Reconstruct the exact clock-bound store/workspace/capture join over persisted records."""
    from dango.operations.store import OperationStore
    from dango.storage.inputs import InputCaptureService
    from dango.storage.workspaces import WorkspaceService

    owner, value = rig.capture()
    store = OperationStore(rig.root, clock_epoch_us=lambda: int(rig.now.timestamp() * 1000000))
    workspaces = WorkspaceService(rig.root, rig.allocator, store, lambda: rig.now)
    inputs = InputCaptureService(rig.root, rig.allocator, workspaces, lambda: rig.now)
    assert inputs.inspect_capture(owner, value.input_set_id) == value
    assert store.inspect_effect(
        owner, owner.operation_id, "inputs.capture"
    ) == rig.store.inspect_effect(owner, owner.operation_id, "inputs.capture")
