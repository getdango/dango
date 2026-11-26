"""tests/integration/input_cases.py

Execute the frozen12 captured-input obligations over populated ordinary-role native workspaces.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from input_observations import (
    capture_id,
    context,
    fixture_edit,
    journal,
    record,
    refused,
    rows,
    schemas,
    selected_estimate,
    tables,
    target,
)
from native_workspaces import child


def test_capture_350_remains_350_after_prod_360(rig):
    """01: retain actual pre-advance350 and capture the same old snapshot after current360."""
    rig.advance(200)
    owner, before = rig.capture()
    rig.advance(210)
    assert rows(rig, owner, rig.targets[-1]) == [(1, 100), (2, 200), (3, 50)]
    assert rig.inputs.inspect_capture(owner, before.input_set_id) == before
    later, after = rig.capture()
    assert rows(rig, later, rig.targets[-1]) == [(1, 100), (2, 200), (3, 50)]
    assert (
        before.catalog_snapshot_ids
        == after.catalog_snapshot_ids
        == {"accepted-production": str(rig.snapshot)}
    )
    with rig.allocator.attach(later, rig.accepted.binding, read_only=True) as connection:
        assert connection.execute("SELECT * FROM catalog.sales.orders ORDER BY id").fetchall() == [
            (1, 100),
            (2, 210),
            (3, 50),
        ]
    return {"before": 350, "after": 350, "current": 360, "snapshot": str(rig.snapshot)}


def test_baseline_and_candidate_share_input_identity(rig):
    """02: two admitted consumers use exactly one durable captured input identity and rows."""
    owner, value = rig.capture()
    workspace = rig.targets[-1]
    observations = []
    for name in ("baseline", "candidate"):
        consumer = rig.admit(name, {"input_set_id": value.input_set_id})
        pin = rig.service.acquire_pin(consumer, workspace.workspace_id, "query", value.input_set_id)
        observed = rig.inputs.inspect_capture(consumer, value.input_set_id)
        assert observed == value and rows(rig, consumer, workspace) == [(1, 100), (2, 200), (3, 50)]
        observations.append(observed.model_dump(mode="json"))
        rig.service.release_pin(consumer, workspace.workspace_id, pin.pin_id)
        rig.finish(consumer)
    assert observations[0] == observations[1]
    return {"one_input_set_id": value.input_set_id, "identical_consumer_snapshots": True}


def test_unselected_relation_absent(rig):
    """03: native enumeration has only the selected original sales relation."""
    owner, value = rig.capture()
    assert tables(rig, owner, rig.targets[-1]) == [("sales", "orders")]
    assert tuple(r.relation_id for r in value.selected_upstream_relations) == ("sales",)
    return {"actual_tables": [["sales", "orders"]], "finance_absent": True}


def test_duplicate_table_names_in_separate_schemas(rig):
    """04: independent literal ordered schemas and350/7 values survive original qualification."""
    owner, value = rig.capture(relations=("sales", "finance"))
    workspace = rig.targets[-1]
    assert rows(rig, owner, workspace) == [(1, 100), (2, 200), (3, 50)]
    assert rows(rig, owner, workspace, "finance") == [(7,)]
    assert tables(rig, owner, workspace) == [("finance", "orders"), ("sales", "orders")]
    expected = schemas(rig, owner, workspace)
    assert {
        r.schema_name: r.schema_fingerprint for r in value.selected_upstream_relations
    } == expected
    measured = sum(
        p.stat().st_size
        for p in Path(rig.native.current(workspace.workspace_id).data_root_ref).rglob("*.parquet")
    )
    assert measured == value.captured_bytes
    return {
        "sales": 350,
        "finance": 7,
        "schema_fingerprints": expected,
        "actual_file_bytes": measured,
    }


def test_quota_denied_before_copy(rig):
    """05: independently measured selected-source estimate denies before any target tables exist."""
    estimate, workspace = selected_estimate(rig), target(rig)
    owner = context(rig, workspace, maximum=estimate - 1)
    refused(
        lambda: rig.capture(context=owner, target=workspace, maximum=estimate - 1),
        "capture_quota_exceeded",
    )
    identity = capture_id(rig, owner)
    raw = record(rig, identity)[1]
    assert (
        raw["estimated_bytes"] == estimate
        and raw["captured_bytes"] == 0
        and raw["phase"] == "failed"
    )
    assert tables(rig, owner, workspace) == []
    assert not list((rig.native.root / ".dango/state/inputs/staging" / identity).glob("*.parquet"))
    return {
        "independent_estimate": estimate,
        "budget": estimate - 1,
        "target_tables": [],
        "journal": journal(rig, owner),
    }


def test_copy_overrun_or_interrupt_never_marks_ready(rig):
    """06: die after an actual committed native write, then refuse recovery without recopy or unpin."""
    workspace = target(rig)
    owner = context(rig, workspace)
    original = rig.inputs.copy.copy_one

    def interrupted():
        def die(*args):
            original(*args)
            os._exit(73)

        rig.inputs.copy.copy_one = die
        rig.capture(context=owner, target=workspace)

    child(interrupted, expected=73)
    identity = capture_id(rig, owner)
    rig.captures.append((owner, identity))
    before = record(rig, identity)[1]
    raw_before = journal(rig, owner)
    assert before["phase"] == "copying" and len(before["pins"]) == 2
    assert json.loads(before["result_json"])["state"] == "preparing"
    assert raw_before["operation"][0][0] == "prepared" and raw_before["effect"] == [
        ("prepared", identity)
    ]
    assert rows(rig, owner, workspace) == [(1, 100), (2, 200), (3, 50)]
    native = rig.native.metadata(rig.native.current(workspace.workspace_id))
    refused(lambda: rig.capture(context=owner, target=workspace), "capture_recovery_required")
    after = record(rig, identity)[1]
    assert after["phase"] == "failed" and after["pins"] == before["pins"]
    refused(lambda: rig.capture(context=owner, target=workspace), "capture_recovery_required")
    assert rig.native.metadata(rig.native.current(workspace.workspace_id)) == native
    return {
        "classification": "real_process_death_after_native_commit",
        "exit": 73,
        "before_json": before,
        "before_sql": raw_before,
        "after_json": after,
        "after_sql": journal(rig, owner),
        "local_injected_overrun": "test_captured_inputs.py::test_copy_overrun_or_interrupt_never_marks_ready",
    }


def test_expired_snapshot_and_changed_schema_are_actionable(rig):
    """07: an actually missing nativeS and an admitted-vs-native schema mismatch never refresh to360."""
    schema = '"' + rig.accepted.binding.metadata_schema + '"'
    saved = rig.native.sql(
        f"SELECT row_to_json(t)::text FROM {schema}.ducklake_snapshot t WHERE snapshot_id={rig.snapshot}"
    )[0][0]
    rig.native.sql(
        f"DELETE FROM {schema}.ducklake_snapshot WHERE snapshot_id={rig.snapshot}", execute=True
    )
    try:
        refused(lambda: rig.capture(), "capture_source_unavailable")
    finally:
        literal = saved.replace("'", "''")
        rig.native.sql(
            f"INSERT INTO {schema}.ducklake_snapshot SELECT * FROM json_populate_record(NULL::{schema}.ducklake_snapshot,'{literal}'::json)",
            execute=True,
        )
    relations = rig.accepted.model_dump(mode="json")["relations"]
    relations[0]["columns"][0]["type"] = "BIGINT"
    import hashlib

    relations[0]["schema_fingerprint"] = hashlib.sha256(
        json.dumps(relations[0]["columns"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with fixture_edit(rig, {"relations": relations}):
        refused(lambda: rig.capture(), "capture_schema_changed")
    assert rig.native.metadata(rig.accepted.binding) == rig.ready_metadata
    return {
        "missing_native_snapshot_refused": True,
        "actual_schema_mismatch_refused": True,
        "no_latest_fallback": True,
    }


def strict_and_replay(rig):
    """08: strict public/private contracts and canonical replay preserve native state and identities."""
    from pydantic import ValidationError

    from dango.operations.contracts import OperationRequest
    from dango.storage.inputs import CapturedRelation, InputSet

    workspace = target(rig)
    owner = context(rig, workspace, key="matrix-canonical")
    proposed = owner.model_copy(update={"operation_id": uuid4().hex})
    replay = rig.store.begin_operation(
        proposed,
        OperationRequest(
            operation_name="inputs.capture",
            payload={
                "workspace_id": workspace.workspace_id,
                "accepted_release_id": "p0-accepted-v1",
                "relation_ids": ["sales"],
                "max_bytes": 1000000,
            },
            idempotency_key="matrix-canonical",
        ),
    )
    assert replay.operation_id == owner.operation_id != proposed.operation_id
    refused(lambda: rig.capture(context=proposed, target=workspace))
    owner, value = rig.capture(context=owner, target=workspace)
    for maximum in (True, -1):
        invalid = context(rig, workspace, maximum=maximum)
        refused(
            lambda maximum=maximum, invalid=invalid: rig.capture(
                context=invalid, target=workspace, maximum=maximum
            ),
            "capture_invalid",
        )
    refused(lambda: rig.capture(relations=("sales", "sales")), "capture_invalid")
    relation = value.selected_upstream_relations[0].model_dump()
    for change in ({"bytes": True}, {"bytes": -1}, {"bytes": float("nan")}, {"extra": 1}):
        try:
            CapturedRelation.model_validate(relation | change)
        except ValidationError:
            pass
        else:
            raise AssertionError("Invalid relation admitted")
    for change in (
        {"estimated_bytes": True},
        {"created_at": "2026-01-01"},
        {"state": "released"},
        {"extra": 1},
    ):
        try:
            InputSet.model_validate(value.model_dump() | change)
        except ValidationError:
            pass
        else:
            raise AssertionError("Invalid input set admitted")
    native = rig.native.metadata(rig.native.current(value.workspace_id))
    assert (
        rig.inputs.capture_inputs(owner, value.workspace_id, "p0-accepted-v1", ("sales",), 1000000)
        == value
    )
    path, _ = record(rig, value.input_set_id)
    original = path.read_bytes()
    try:
        path.write_text('{"unknown":true}')
        refused(lambda: rig.inputs.inspect_capture(owner, value.input_set_id))
    finally:
        path.write_bytes(original)
    assert rig.native.metadata(rig.native.current(value.workspace_id)) == native
    refused(lambda: rig.capture(target=workspace), "capture_collision")
    assert rows(rig, owner, workspace) == [(1, 100), (2, 200), (3, 50)]
    return {
        "canonical_input_set_id": value.input_set_id,
        "strict_records": True,
        "collision_preserves_original": True,
    }


def owner_and_source(rig):
    """09: no branch/context possession substitutes for admission/current source grants or credentials."""
    workspace = target(rig)
    owner = context(rig, workspace)
    before = rig.native.control["resolves"]
    for changes in (
        {"actor_id": "foreign"},
        {"project_id": "foreign"},
        {"operation_id": uuid4().hex},
    ):
        refused(
            lambda changes=changes: rig.capture(
                context=owner.model_copy(update=changes), target=workspace
            )
        )
    assert rig.native.control["resolves"] == before
    refused(lambda: rig.capture(relations=("unknown",)))
    valid, value = rig.capture()
    rig.native.control["grant"] = False
    try:
        refused(lambda: rig.inputs.inspect_capture(valid, value.input_set_id))
    finally:
        rig.native.control["grant"] = True
    current = rig.native.control["current"]
    rig.native.control["current"] = current.model_copy(update={"version": "revoked"})
    try:
        refused(lambda: rig.inputs.inspect_capture(valid, value.input_set_id))
    finally:
        rig.native.control["current"] = current
    descriptor = rig.accepted.binding.model_dump(mode="json") | {
        "data_root_ref": str(rig.native.root / "unknown")
    }
    with fixture_edit(rig, {"binding": descriptor}):
        refused(lambda: rig.capture())
    return {
        "wrong_owner_unadmitted_denied_before_resolve": True,
        "revocation_credential_descriptor_denied": True,
    }


def fixture_lifetime(rig):
    """10: actual live lock loss and unknown/active/managed maintenance markers fail closed."""
    owner, value = rig.capture()
    path = rig.native.root / ".dango/state/inputs/lifetime.lock"
    held = path.with_suffix(".held")
    original = path.read_bytes()
    path.rename(held)
    try:
        path.write_bytes(original)
        path.chmod(0o600)
        refused(
            lambda: rig.inputs.inspect_capture(owner, value.input_set_id),
            "capture_source_unavailable",
        )
    finally:
        path.unlink()
        held.rename(path)
    for changes in (
        {"maintenance_mode": "active"},
        {"maintenance_mode": "unknown"},
        {"retention_mode": "managed"},
    ):
        with fixture_edit(rig, changes):
            refused(lambda: rig.inputs.inspect_capture(owner, value.input_set_id))
    assert rig.inputs.inspect_capture(owner, value.input_set_id) == value
    return {
        "real_unheld_replacement_refused": True,
        "maintenance_modes_refused": True,
        "actual_read_only_maintenance": "compulsory populated smoke",
    }


def release_consumers(rig):
    """11: original-context partial release is retryable and preserves every other consumer/receipt."""
    owner, value = rig.capture()
    workspace = rig.targets[-1]
    consumers = []
    for purpose in ("input", "query", "build", "backup"):
        consumer = rig.admit("consumer." + purpose, {})
        pin = rig.service.acquire_pin(consumer, workspace.workspace_id, purpose, value.input_set_id)
        consumers.append((consumer, pin))
    before = journal(rig, owner)
    refused(
        lambda: rig.inputs.release_capture(consumers[0][0], value.input_set_id), "capture_denied"
    )
    original = rig.service.release_pin
    calls = []

    def partial(*args):
        calls.append(args[-1])
        if len(calls) == 2:
            raise InterruptedError()
        return original(*args)

    rig.service.release_pin = partial
    try:
        refused(lambda: rig.inputs.release_capture(owner, value.input_set_id))
    finally:
        rig.service.release_pin = original
    rig.inputs.release_capture(owner, value.input_set_id)
    assert journal(rig, owner) == before
    assert (
        rig.inputs.capture_inputs(
            owner, workspace.workspace_id, "p0-accepted-v1", ("sales",), 1000000
        )
        == value
    )
    refused(lambda: rig.inputs.inspect_capture(owner, value.input_set_id), "capture_released")
    from datetime import datetime, timedelta

    rig.now = datetime.fromisoformat(workspace.lease_expiry) + timedelta(seconds=1)
    assert rig.expire(workspace).error_code == "workspace_retained"
    for consumer, pin in consumers:
        rig.service.release_pin(consumer, workspace.workspace_id, pin.pin_id)
    assert rig.expire(workspace).error_code == "workspace_retained"
    for consumer, _ in consumers:
        rig.finish(consumer)
    rig.dispose(workspace)
    rig.targets.remove(workspace)
    return {
        "partial_release_retry": True,
        "four_consumer_purposes_retained": True,
        "active_operations_retained": True,
        "last_eligible_expiry": True,
    }


def durability(rig):
    """12: fresh process reopens exact saved identity, then missing/corrupt late authority denies."""
    owner, value = rig.capture()
    output = rig.native.root / "fresh-input.json"

    def reopen():
        from dango.operations.store import OperationStore
        from dango.storage.inputs import InputCaptureService
        from dango.storage.workspaces import WorkspaceService

        store = OperationStore(
            rig.native.root, clock_epoch_us=lambda: int(rig.now.timestamp() * 1000000)
        )
        workspaces = WorkspaceService(rig.native.root, rig.allocator, store, lambda: rig.now)
        inputs = InputCaptureService(rig.native.root, rig.allocator, workspaces, lambda: rig.now)
        assert inputs.inspect_capture(owner, value.input_set_id) == value
        assert rows(rig, owner, rig.targets[-1]) == [(1, 100), (2, 200), (3, 50)]
        output.write_text(value.model_dump_json())

    child(reopen)
    assert json.loads(output.read_text()) == value.model_dump(mode="json")
    manifest = rig.native.root / ".dango/state/inputs/accepted.json"
    before = manifest.read_bytes()
    native = rig.native.metadata(rig.accepted.binding)
    for malformed in (None, b'{"unknown":true}'):
        try:
            if malformed is None:
                manifest.unlink()
            else:
                manifest.write_bytes(malformed)
            refused(lambda: rig.inputs.inspect_capture(owner, value.input_set_id))
        finally:
            manifest.write_bytes(before)
            manifest.chmod(0o600)
    assert rig.native.metadata(rig.accepted.binding) == native
    return {
        "fresh_process_exact_result": True,
        "missing_corrupt_late_authority_denied": True,
        "import_resource_cleanup": "required workflow and final receipt gates",
    }


CASES = tuple(
    (f"{index:02d}", case)
    for index, case in enumerate(
        (
            test_capture_350_remains_350_after_prod_360,
            test_baseline_and_candidate_share_input_identity,
            test_unselected_relation_absent,
            test_duplicate_table_names_in_separate_schemas,
            test_quota_denied_before_copy,
            test_copy_overrun_or_interrupt_never_marks_ready,
            test_expired_snapshot_and_changed_schema_are_actionable,
            strict_and_replay,
            owner_and_source,
            fixture_lifetime,
            release_consumers,
            durability,
        ),
        1,
    )
)
