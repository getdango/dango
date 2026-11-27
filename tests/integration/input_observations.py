"""tests/integration/input_observations.py

Keep independent native fixture, raw journal/file and strict refusal oracles for the finite12 cases.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dango.storage.input_state import AcceptedFixture, InputCaptureError


def refused(action, code=None):
    """Require an actual fixed capture refusal, never a generic native/test exception."""
    try:
        action()
    except InputCaptureError as error:
        assert error.code.startswith("capture_") and "password" not in str(error)
        if code:
            assert error.code == code
        return error.code
    raise AssertionError("Expected safe capture refusal")


def target(rig):
    """Track a fresh workspace so positive absence/collision answers have an independent scope."""
    _, workspace = rig.allocate()
    rig.targets.append(workspace)
    return workspace


def context(rig, workspace, relations=("sales",), maximum=1000000, *, key=None):
    """Admit the fixed exact public capture arguments, using the canonical returned operation ID."""
    return rig.admit(
        "inputs.capture",
        {
            "workspace_id": workspace.workspace_id,
            "accepted_release_id": "p0-accepted-v1",
            "relation_ids": list(relations),
            "max_bytes": maximum,
        },
        key=key,
    )


def record(rig, identity):
    """Read the durable capture JSON independently of the service's returned InputSet."""
    path = rig.native.root / ".dango/state/inputs/captures" / (identity + ".json")
    return path, json.loads(path.read_text())


def capture_id(rig, owner):
    """Read exact effect identity through an independent SQLite connection."""
    return rig.rows(
        "SELECT native_ref FROM platform_effects WHERE operation_id=? AND effect_name='inputs.capture'",
        (owner.operation_id,),
    )[0][0]


def tables(rig, owner, workspace):
    """Enumerate actual native target names without trusting InputSet selected metadata."""
    binding = rig.native.current(workspace.workspace_id)
    with rig.allocator.attach(owner, binding, read_only=True) as connection:
        return connection.execute(
            "SELECT table_schema,table_name FROM information_schema.tables WHERE table_catalog='catalog' ORDER BY 1,2"
        ).fetchall()


def rows(rig, owner, workspace, schema="sales"):
    """Read only the two fixed original fixture schemas, independently checking literal values."""
    assert schema in {"sales", "finance"}
    binding = rig.native.current(workspace.workspace_id)
    with rig.allocator.attach(owner, binding, read_only=True) as connection:
        return connection.execute(
            'SELECT * FROM catalog."' + schema + '".orders ORDER BY 1'
        ).fetchall()


def schemas(rig, owner, workspace):
    """Compare actual ordered native schemas to explicit contract literals."""
    binding = rig.native.current(workspace.workspace_id)
    expected = {
        "sales": [("id", "INTEGER", "YES"), ("amount", "INTEGER", "YES")],
        "finance": [("amount", "BIGINT", "YES")],
    }
    fingerprints = {}
    with rig.allocator.attach(owner, binding, read_only=True) as connection:
        for schema, columns in expected.items():
            actual = connection.execute(
                'DESCRIBE SELECT * FROM catalog."' + schema + '".orders'
            ).fetchall()
            assert [r[:3] for r in actual] == columns
            literal = [
                {"name": name, "type": kind, "nullable": nullable == "YES"}
                for name, kind, nullable in columns
            ]
            fingerprints[schema] = hashlib.sha256(
                json.dumps(literal, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
    return fingerprints


def selected_estimate(rig):
    """Observe selected-S file sizes directly in PostgreSQL and stat each original source file."""
    binding, snapshot = rig.accepted.binding, rig.snapshot
    schema = '"' + binding.metadata_schema + '"'
    data = rig.native.sql(
        "SELECT s.path,t.path,f.path,f.file_size_bytes FROM "
        + schema
        + ".ducklake_data_file f JOIN "
        + schema
        + ".ducklake_table t USING(table_id) JOIN "
        + schema
        + f".ducklake_schema s USING(schema_id) WHERE s.schema_name='sales' AND t.table_name='orders' AND f.begin_snapshot<={snapshot} AND (f.end_snapshot IS NULL OR f.end_snapshot>{snapshot})"
    )
    assert data
    total = 0
    for sp, tp, fp, size in data:
        path = Path(binding.data_root_ref) / sp / tp / fp
        assert path.is_relative_to(binding.data_root_ref) and path.stat().st_size == size
        total += size
    return total


@contextmanager
def fixture_edit(rig, changes):
    """Install a trusted-fixture negative while retaining the real held lock; restore exact bytes."""
    root = rig.native.root / ".dango/state/inputs"
    manifest, guard = root / "accepted.json", root / "lifetime.lock"
    before, header = manifest.read_bytes(), guard.read_bytes()
    value = rig.accepted.model_dump(mode="json") | changes
    body = json.dumps(value).encode()
    try:
        try:
            digest = hashlib.sha256(
                AcceptedFixture.model_validate_json(body).model_dump_json().encode()
            ).hexdigest()
        except ValueError:
            digest = hashlib.sha256(body).hexdigest()
        guardian = json.loads(header)
        guardian["manifest_sha256"] = digest
        manifest.write_bytes(body)
        guard.write_text(json.dumps(guardian))
        yield
    finally:
        manifest.write_bytes(before)
        guard.write_bytes(header)


def journal(rig, owner):
    """Retain raw canonical lifecycle/effect/receipt rows for crash/replay assertions."""
    args = (owner.operation_id,)
    return {
        "operation": rig.rows(
            "SELECT state,result_ref,status FROM platform_operations WHERE operation_id=?", args
        ),
        "effect": rig.rows(
            "SELECT state,native_ref FROM platform_effects WHERE operation_id=?", args
        ),
        "receipt": rig.rows(
            "SELECT status,result_ref,outcome_json FROM platform_operations WHERE operation_id=?",
            args,
        ),
    }


AFFECTED = ("04",)
RETAINED = {
    "head": "fa2282c0bba6f959ca9a2d12d314cecc395c46df",
    "run": "45861634718",
    "retained_case_ids": ["01", "02", "03", "04"],
    "cases": {
        f"{n:02d}": ("PASS" if n <= 4 else ("FAIL" if n == 5 else "UNRUN")) for n in range(1, 13)
    },
}

RETAINED_AFFECTED = {
    "head": "333033f5597cae3968810af3d15eb8d879bb97ba",
    "run": "45863495362",
    "retained_case_ids": [f"{n:02d}" for n in range(5, 12)],
    "cases": {
        f"{n:02d}": ("UNRUN" if n < 5 else ("FAIL" if n == 12 else "PASS")) for n in range(1, 13)
    },
}


RETAINED_DURABILITY = {
    "head": "64de432c7cd637af997fcff2d063070b8f5a6fb9",
    "run": "45864177842",
    "retained_case_ids": ["12"],
    "cases": {f"{n:02d}": ("PASS" if n == 12 else "UNRUN") for n in range(1, 13)},
}


def authority_snapshot(rig):
    """Compare every persisted JSON/pin, raw journal row, data/staging file and resolver use."""
    return {
        "json": {
            str(p.relative_to(rig.native.root)): p.read_text()
            for p in rig.native.root.rglob("*.json")
        },
        "operations": rig.rows("SELECT * FROM platform_operations ORDER BY operation_id"),
        "effects": rig.rows("SELECT * FROM platform_effects ORDER BY operation_id,effect_id"),
        "parquet": {
            str(p.relative_to(rig.native.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in rig.native.root.rglob("*.parquet")
        },
        "resolves": rig.native.control["resolves"],
    }


def lookup_corruption(rig, operation):
    """G1: real valid B stored under A never changes the external lookup or B's retained pins."""
    owner_a, a = rig.capture()
    owner_b, b = rig.capture()
    path, _ = record(rig, a.input_set_id)
    original = path.read_bytes()
    path.write_bytes(record(rig, b.input_set_id)[0].read_bytes())
    try:
        before = authority_snapshot(rig)
        action = (
            (lambda: rig.inputs.inspect_capture(owner_a, a.input_set_id))
            if operation == "inspect"
            else (lambda: rig.inputs.release_capture(owner_b, a.input_set_id))
        )
        code = refused(action, "capture_recovery_required")
        after = authority_snapshot(rig)
        assert after == before
        evidence = {
            "operation": operation,
            "lookup": a.input_set_id,
            "misplaced": b.input_set_id,
            "code": code,
            "before": before,
            "after": after,
        }
    finally:
        path.write_bytes(original)
    rig.inputs.release_capture(owner_b, b.input_set_id)
    assert (
        rig.inputs.capture_inputs(owner_b, b.workspace_id, "p0-accepted-v1", ("sales",), 1000000)
        == b
    )
    assert rig.inputs.inspect_capture(owner_a, a.input_set_id) == a
    return evidence


def success_corruption(rig, owner, value):
    """G2: preserve exact SQL/JSON/pins/files while rejecting both reproduced success contradictions."""
    database = rig.native.root / ".dango/dango.db"
    with sqlite3.connect(database) as connection:
        saved = connection.execute(
            "SELECT result_ref,outcome_json FROM platform_operations WHERE operation_id=?",
            (owner.operation_id,),
        ).fetchone()
    evidence = []
    for mutation in ("prepared_success_effect", "wrong_success_result"):
        try:
            with sqlite3.connect(database) as connection:
                if mutation == "prepared_success_effect":
                    connection.execute(
                        "UPDATE platform_effects SET state='prepared' WHERE operation_id=?",
                        (owner.operation_id,),
                    )
                else:
                    receipt = json.loads(saved[1])
                    receipt["result_ref"] = "different-input-id"
                    connection.execute(
                        "UPDATE platform_operations SET result_ref=?,outcome_json=? WHERE operation_id=?",
                        ("different-input-id", json.dumps(receipt), owner.operation_id),
                    )
            before = authority_snapshot(rig)
            codes = [
                refused(
                    lambda: rig.inputs.capture_inputs(
                        owner, value.workspace_id, "p0-accepted-v1", ("sales",), 1000000
                    ),
                    "capture_recovery_required",
                ),
                refused(
                    lambda: rig.inputs.inspect_capture(owner, value.input_set_id),
                    "capture_recovery_required",
                ),
                refused(
                    lambda: rig.inputs.release_capture(owner, value.input_set_id),
                    "capture_recovery_required",
                ),
            ]
            after = authority_snapshot(rig)
            assert after == before
            evidence.append(
                {"mutation": mutation, "codes": codes, "before": before, "after": after}
            )
        finally:
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE platform_effects SET state='applied' WHERE operation_id=?",
                    (owner.operation_id,),
                )
                connection.execute(
                    "UPDATE platform_operations SET result_ref=?,outcome_json=? WHERE operation_id=?",
                    (*saved, owner.operation_id),
                )
    assert rig.inputs.inspect_capture(owner, value.input_set_id) == value
    return evidence


def prepared_ready_recovery(rig):
    """Keep a real completed native copy active/prepared, then finalize exactly once without recopy."""
    workspace = target(rig)
    owner = context(rig, workspace)
    original = rig.store.record_effect

    def before_applied(caller, operation, effect):
        if effect.name == "inputs.capture" and effect.state == "applied":
            raise InterruptedError()
        return original(caller, operation, effect)

    rig.store.record_effect = before_applied
    try:
        refused(lambda: rig.capture(context=owner, target=workspace), "capture_recovery_required")
    finally:
        rig.store.record_effect = original
    identity = capture_id(rig, owner)
    before = {"json": record(rig, identity)[1], "sql": journal(rig, owner)}
    assert before["json"]["phase"] == "ready" and before["sql"]["operation"][0][0] == "prepared"
    assert before["sql"]["effect"] == [("prepared", identity)]
    refused(lambda: rig.inputs.inspect_capture(owner, identity), "capture_recovery_required")
    native = rig.native.metadata(rig.native.current(workspace.workspace_id))
    _, value = rig.capture(context=owner, target=workspace)
    assert value.input_set_id == identity and value.state == "ready"
    assert rig.native.metadata(rig.native.current(workspace.workspace_id)) == native
    return {
        "before": before,
        "after": {"json": record(rig, identity)[1], "sql": journal(rig, owner)},
        "native_unchanged": True,
    }


RETAINED_CORRECTION = {
    "head": "b63f5ea72b5f47c43be5b94075bfb0c902518a9c",
    "run": "45929522151",
    "retained_case_ids": ["08", "11", "12"],
    "cases": {f"{n:02d}": ("PASS" if n in {8, 11, 12} else "UNRUN") for n in range(1, 13)},
}


def direct_active_cleanup(rig):
    """B1/11: release after real post-commit process death, without a capture recovery call."""
    import os
    from datetime import datetime, timedelta

    from native_workspaces import child

    workspace = target(rig)
    owner = context(rig, workspace)
    original_copy = rig.inputs.copy.copy_one

    def interrupted():
        def die(*args):
            original_copy(*args)
            os._exit(73)

        rig.inputs.copy.copy_one = die
        rig.capture(context=owner, target=workspace)

    child(interrupted, expected=73)
    identity = capture_id(rig, owner)
    rig.captures.append((owner, identity))
    consumer = rig.admit("consumer.direct-cleanup", {})
    pin = rig.service.acquire_pin(consumer, workspace.workspace_id, "query", identity)

    def snapshot():
        return {
            "json": record(rig, identity)[1],
            "sql": journal(rig, owner),
            "source_pins": rig.manifest(rig.source.workspace_id)[1]["pins"],
            "target_pins": rig.manifest(workspace.workspace_id)[1]["pins"],
        }

    before = snapshot()
    assert before["json"]["phase"] == "copying"
    assert before["sql"]["operation"] == [("prepared", None, None)]
    assert before["sql"]["effect"] == [("prepared", identity)]
    assert rows(rig, owner, workspace) == [(1, 100), (2, 200), (3, 50)]
    native = rig.native.metadata(rig.native.current(workspace.workspace_id))
    original_release, original_finalize = rig.service.release_pin, rig.store.finalize_operation
    calls = []

    def partial(*args):
        calls.append(args[-1])
        if len(calls) == 2:
            raise InterruptedError()
        return original_release(*args)

    rig.service.release_pin = partial
    try:
        refused(lambda: rig.inputs.release_capture(owner, identity), "capture_unavailable")
    finally:
        rig.service.release_pin = original_release
    partial_state = snapshot()
    assert partial_state["json"]["phase"] == "releasing"

    def finalized_then_interrupted(*args):
        original_finalize(*args)
        raise InterruptedError()

    rig.store.finalize_operation = finalized_then_interrupted
    try:
        refused(lambda: rig.inputs.release_capture(owner, identity), "capture_unavailable")
    finally:
        rig.store.finalize_operation = original_finalize
    finalized = snapshot()
    assert finalized["json"]["phase"] == "releasing"
    assert finalized["sql"]["operation"] == [("finalized", identity, "cancelled")]
    receipt = json.loads(finalized["sql"]["receipt"][0][2])
    assert receipt["error_code"] == "capture_released"
    assert (
        receipt["action"] == "Admit a new capture before using released historical input metadata."
    )
    rig.inputs.release_capture(owner, identity)
    after = snapshot()
    assert after["json"]["phase"] == "released" and after["sql"] == finalized["sql"]
    for entries in (after["source_pins"], after["target_pins"]):
        assert all(p["released"] for p in entries if p["pin"]["operation_id"] == owner.operation_id)
    assert any(p["pin"]["pin_id"] == pin.pin_id and not p["released"] for p in after["target_pins"])
    unchanged = authority_snapshot(rig)
    rig.inputs.release_capture(owner, identity)
    assert authority_snapshot(rig) == unchanged
    assert rig.native.metadata(rig.native.current(workspace.workspace_id)) == native
    assert not list((rig.native.root / ".dango/state/inputs/staging" / identity).glob("*.parquet"))
    rig.now = datetime.fromisoformat(workspace.lease_expiry) + timedelta(seconds=1)
    assert rig.expire(workspace).error_code == "workspace_retained"
    rig.service.release_pin(consumer, workspace.workspace_id, pin.pin_id)
    assert rig.expire(workspace).error_code == "workspace_retained"
    rig.finish(consumer)
    rig.dispose(workspace)
    rig.targets.remove(workspace)
    return {
        "classification": "real_process_death_after_native_commit_then_direct_release",
        "exit": 73,
        "before": before,
        "partial": partial_state,
        "finalized_before_tombstone": finalized,
        "after": after,
        "idempotent_no_reacquire_or_recopy": True,
        "other_pin_then_active_operation_retained": True,
        "last_eligible_owned_expiry": True,
    }


RETAINED_CLEANUP = {
    "head": "9ea2a93b939fbc6cce93e81721e22e761a61771e",
    "run": "45931116545",
    "retained_case_ids": ["11"],
    "cases": {f"{n:02d}": ("PASS" if n == 11 else "UNRUN") for n in range(1, 13)},
}


RETAINED_AST = {
    "head": "88997b69d0979face58cdbfba8703875df5415ae",
    "run": "45957026257",
    "overall_status": "FAIL",
    "retained_case_ids": ["03", "05", "07", "08"],
    "cases": {f"{n:02d}": ("PASS" if n in {3, 4, 5, 7, 8} else "UNRUN") for n in range(1, 13)},
}
