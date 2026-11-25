"""tests/unit/test_operation_store_workspace.py

Check owner-scoped inspection against independent SQLite rows and unchanged write ownership.
"""

import sqlite3

import pytest

from dango.operations.contracts import OutcomeReceipt
from dango.operations.store import EffectRecord
from tests.unit.workspace_support import Rig

pytestmark = pytest.mark.unit


def test_inspect_effect_absence_exact_record_and_final_receipt(tmp_path):
    rig = Rig(tmp_path)
    target = rig.admit("query", {})
    caller = rig.admit("expiry", {})
    snapshot = rig.store.inspect_effect(caller, target.operation_id, "capture")
    assert snapshot.state == "prepared" and snapshot.effect is None and snapshot.receipt is None
    effect = EffectRecord(
        effect_id="capture", sequence=0, name="capture", state="prepared", native_ref="native"
    )
    rig.store.record_effect(target, target.operation_id, effect)
    snapshot = rig.store.inspect_effect(caller, target.operation_id, "capture")
    raw = rig.rows(
        "SELECT effect_id,effect_sequence,effect_name,state,native_ref,recovery_ref FROM platform_effects"
    )[0]
    assert tuple(snapshot.effect.model_dump().values()) == raw
    receipt = rig.store.finalize_operation(
        target,
        OutcomeReceipt(
            operation_id=target.operation_id,
            status="success",
            result_ref="result",
            resource_revision="3",
        ),
    )
    assert rig.store.inspect_effect(caller, target.operation_id, "capture").receipt == receipt
    rig.finish(caller)
    assert rig.store.inspect_effect(caller, target.operation_id, "capture").state == "finalized"


@pytest.mark.parametrize("owner", [{"actor_id": "other"}, {"project_id": "other"}])
def test_foreign_and_unadmitted_inspection_denied(tmp_path, owner):
    rig = Rig(tmp_path)
    target = rig.admit("query", {})
    foreign = rig.admit("query", {}, context=rig.context(**owner))
    for caller, operation in (
        (foreign, target.operation_id),
        (target, foreign.operation_id),
        (rig.context(), target.operation_id),
        (target, "absent"),
    ):
        with pytest.raises(ValueError, match="operation_denied"):
            rig.store.inspect_effect(caller, operation, "capture")


def test_inspection_does_not_loosen_mutating_ownership(tmp_path):
    rig = Rig(tmp_path)
    target = rig.admit("query", {})
    caller = rig.admit("query", {})
    before = rig.rows("SELECT * FROM platform_operations ORDER BY operation_id")
    rig.store.inspect_effect(caller, target.operation_id, "capture")
    with pytest.raises(ValueError, match="operation_denied"):
        rig.store.mark_running(caller, target.operation_id)
    with pytest.raises(ValueError, match="operation_denied"):
        rig.store.record_effect(
            caller,
            target.operation_id,
            EffectRecord(effect_id="capture", name="capture", sequence=0, state="prepared"),
        )
    assert rig.rows("SELECT * FROM platform_operations ORDER BY operation_id") == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("result_ref", "other"),
        ("resource_revision", "other"),
        ("outcome_json", "{}"),
        ("request_digest", "wrong"),
    ],
)
def test_inconsistent_operation_rows_refused(tmp_path, field, value):
    rig = Rig(tmp_path)
    target = rig.admit("query", {})
    rig.store.finalize_operation(
        target,
        OutcomeReceipt(
            operation_id=target.operation_id,
            status="success",
            result_ref="result",
            resource_revision="3",
        ),
    )
    with sqlite3.connect(tmp_path / ".dango/dango.db") as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(f"UPDATE platform_operations SET {field}=?", (value,))
    with pytest.raises(ValueError, match="store_unavailable"):
        rig.store.inspect_effect(target, target.operation_id, "capture")


@pytest.mark.parametrize(
    "field,value",
    [("state", "unknown-state"), ("effect_sequence", -1), ("effect_name", ""), ("native_ref", "")],
)
def test_malformed_effect_rows_refused(tmp_path, field, value):
    rig = Rig(tmp_path)
    target = rig.admit("query", {})
    rig.store.record_effect(
        target,
        target.operation_id,
        EffectRecord(effect_id="capture", name="capture", sequence=0, state="prepared"),
    )
    with sqlite3.connect(tmp_path / ".dango/dango.db") as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(f"UPDATE platform_effects SET {field}=?", (value,))
    with pytest.raises(ValueError, match="store_unavailable"):
        rig.store.inspect_effect(target, target.operation_id, "capture")


@pytest.mark.parametrize("value", [True, 1, "", "\n", "x" * 257, None])
def test_invalid_lookup_input_refused(tmp_path, value):
    rig = Rig(tmp_path)
    caller = rig.admit("query", {})
    with pytest.raises(ValueError, match="invalid_input"):
        rig.store.inspect_effect(caller, caller.operation_id, value)


def test_nonfinalized_row_with_final_outcome_is_not_active_authority(tmp_path):
    rig = Rig(tmp_path)
    target = rig.admit("query", {})
    with sqlite3.connect(tmp_path / ".dango/dango.db") as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE platform_operations SET status='success',outcome_json='{}'")
    with pytest.raises(ValueError, match="store_unavailable"):
        rig.store.inspect_effect(target, target.operation_id, "capture")
