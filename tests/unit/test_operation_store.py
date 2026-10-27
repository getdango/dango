"""tests/unit/test_operation_store.py
Verify durable journal admission, immutable outcomes, effects and server-timed leases.
"""

import json
import os
import sqlite3
import subprocess
import sys

import pytest
from pydantic import ValidationError

from dango.operations.contracts import OperationContext, OperationRequest, OutcomeReceipt
from dango.operations.store import BeginResult, EffectRecord, LeaseBinding, OperationStore

pytestmark = pytest.mark.unit
_CONTEXT = {
    "operation_id": "op-1",
    "project_id": "p1",
    "actor_id": "a1",
    "capability": "read",
    "permission_revision": "r1",
    "trace_id": "t1",
}
_RECEIPT = '{"schema_version":1,"operation_id":"op-1","status":"success","resource_revision":null,"result_ref":"safe-ref","evidence_refs":[],"error_code":null,"action":null}'


def _context(**changes):
    return OperationContext(**{**_CONTEXT, **changes})


def _request(**changes):
    return OperationRequest(
        **{**{"operation_name": "read", "payload": {"n": 1}, "idempotency_key": "k"}, **changes}
    )


def _effect(**changes):
    return EffectRecord(
        **{**{"effect_id": "e1", "sequence": 0, "name": "write", "state": "prepared"}, **changes}
    )


def _rows(root, query, args=()):
    with sqlite3.connect(root / ".dango/dango.db") as conn:
        return conn.execute(query, args).fetchall()


@pytest.fixture
def journal(tmp_path):
    now = [1_000_000]
    return OperationStore(tmp_path, clock_epoch_us=lambda: now[0]), now, tmp_path


def _begin(journal):
    store, now, root = journal
    assert store.begin_operation(_context(), _request()).state == "started"
    return store, now, root


_CHILD = """
import json,sys,time
from pathlib import Path
from dango.operations.contracts import OperationContext,OperationRequest
from dango.operations.store import EffectRecord,OperationStore
root=Path(sys.argv[1])
ctx=OperationContext(operation_id=sys.argv[2],project_id="p1",actor_id="a1",
 capability="read",permission_revision="r1",trace_id="t1")
store=OperationStore(root,clock_epoch_us=lambda:1000000)
print("ready",flush=True)
sys.stdin.readline()
result=store.begin_operation(ctx,OperationRequest(operation_name="read",payload={"n":1},idempotency_key=None if sys.argv[3]=="lease" else "k"))
if sys.argv[3]=="lease":
 try:
  result=store.acquire_lease(ctx,ctx.operation_id,1).model_dump()
 except ValueError as exc:
  result={"code":exc.code}
 print(json.dumps(result),flush=True)
 sys.exit(0)
if sys.argv[3]=="interrupt":
 store.mark_running(ctx,ctx.operation_id)
 store.record_effect(ctx,ctx.operation_id,EffectRecord(effect_id="e1",sequence=0,name="write",state="prepared"))
 print("durable",flush=True)
 sys.stdin.readline()
else:
 print(result.model_dump_json(),flush=True)
"""


def _child(root, operation_id="op-1", mode="begin"):
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(root), operation_id, mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )


def test_same_key_concurrent_begin_admits_one(tmp_path):
    children = [_child(tmp_path, f"op-{n}") for n in range(4)]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = []
        for child in children:
            out, err = child.communicate(timeout=20)
            assert child.returncode == 0, err
            results.append(json.loads(out))
        assert sorted(item["state"] for item in results) == ["in_flight"] * 3 + ["started"]
        rows = _rows(
            tmp_path, "SELECT operation_id,state,status,outcome_json FROM platform_operations"
        )
        assert rows == [(results[0]["operation_id"], "prepared", None, None)]
        assert {item["operation_id"] for item in results} == {rows[0][0]}
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def test_same_key_changed_payload_conflicts(journal):
    store, _, root = _begin(journal)
    result = store.begin_operation(_context(operation_id="retry"), _request(payload={"n": 2}))
    assert (result.state, result.operation_id, result.receipt) == (
        "idempotency_conflict",
        "op-1",
        None,
    )
    assert _rows(root, "SELECT count(*) FROM platform_operations") == [(1,)]


def test_finalized_receipt_replays_exactly(journal):
    store, _, root = _begin(journal)
    receipt = OutcomeReceipt.model_validate_json(_RECEIPT)
    assert store.finalize_operation(_context(), receipt).model_dump_json() == _RECEIPT
    result = store.begin_operation(_context(operation_id="retry"), _request())
    assert (result.state, result.operation_id) == ("replay", "op-1")
    assert result.receipt.model_dump_json() == _RECEIPT
    assert _rows(root, "SELECT state,status,outcome_json FROM platform_operations") == [
        ("finalized", "success", _RECEIPT)
    ]


def test_running_has_no_final_receipt(journal):
    store, _, root = _begin(journal)
    store.mark_running(_context(), "op-1")
    assert _rows(root, "SELECT state,status,outcome_json FROM platform_operations") == [
        ("running", None, None)
    ]
    assert store.begin_operation(_context(), _request()).receipt is None
    with pytest.raises(ValueError, match="operation_conflict"):
        store.mark_running(_context(), "op-1")


def test_final_receipt_cannot_be_rewritten(journal):
    store, _, root = _begin(journal)
    receipt = OutcomeReceipt.model_validate_json(_RECEIPT)
    store.finalize_operation(_context(), receipt)
    assert store.finalize_operation(_context(), receipt) == receipt
    before = _rows(root, "SELECT * FROM platform_operations")
    for call in (
        lambda: store.finalize_operation(
            _context(),
            OutcomeReceipt(
                operation_id="op-1", status="cancelled", error_code="cancelled", action="Retry."
            ),
        ),
        lambda: store.mark_running(_context(), "op-1"),
        lambda: store.record_effect(_context(), "op-1", _effect()),
    ):
        with pytest.raises(ValueError, match="operation_conflict"):
            call()
    assert _rows(root, "SELECT * FROM platform_operations") == before


def test_effect_order_and_duplicate_guard(journal):
    store, _, root = _begin(journal)
    with pytest.raises(ValueError, match="effect_conflict"):
        store.record_effect(_context(), "op-1", _effect(state="applied"))
    store.record_effect(_context(), "op-1", _effect())
    store.record_effect(_context(), "op-1", _effect())
    store.record_effect(_context(), "op-1", _effect(state="applied", native_ref="native-1"))
    for effect in (
        _effect(),
        _effect(effect_id="other"),
        _effect(sequence=1),
        _effect(effect_id="other", sequence=0),
    ):
        with pytest.raises(ValueError, match="effect_conflict"):
            store.record_effect(_context(), "op-1", effect)
    store.record_effect(_context(), "op-1", _effect(effect_id="e2", sequence=2))
    with pytest.raises(ValueError, match="effect_conflict"):
        store.record_effect(_context(), "op-1", _effect(effect_id="e3", sequence=1))
    store.record_effect(
        _context(),
        "op-1",
        _effect(state="compensated", native_ref="native-1", recovery_ref="recovery-op"),
    )
    assert _rows(
        root,
        "SELECT effect_id,effect_sequence,state,native_ref,recovery_ref FROM platform_effects ORDER BY effect_sequence",
    ) == [("e1", 0, "compensated", "native-1", "recovery-op"), ("e2", 2, "prepared", None, None)]


def test_expired_lease_takeover_rejects_old_token(journal):
    store, now, root = _begin(journal)
    old = store.acquire_lease(_context(), "op-1", 2)
    assert (old.revision, old.expires_at_epoch_us) == (0, 3_000_000)
    now[0] = 2_000_000
    renewed = store.renew_lease(_context(), old, 2)
    assert (renewed.revision, renewed.expires_at_epoch_us) == (1, 4_000_000)
    with pytest.raises(ValueError, match="lease_conflict"):
        store.release_lease(_context(), old)
    now[0] = 4_000_000
    for call in (
        lambda: store.renew_lease(_context(), renewed, 2),
        lambda: store.release_lease(_context(), renewed),
    ):
        with pytest.raises(ValueError, match="lease_conflict"):
            call()
    ctx = _context(operation_id="op-2")
    store.begin_operation(ctx, _request(idempotency_key=None))
    new = store.acquire_lease(ctx, "op-2", 3)
    assert (new.revision, new.expires_at_epoch_us) == (2, 7_000_000)
    assert new.token != old.token and len(new.token) >= 32
    with pytest.raises(ValueError, match="lease_conflict"):
        store.renew_lease(_context(), renewed, 2)
    store.release_lease(ctx, new)
    assert _rows(root, "SELECT revision,expires_at_epoch_us FROM platform_leases") == [
        (2, 4_000_000)
    ]
    again = store.acquire_lease(ctx, "op-2", 1)
    assert (again.revision, again.expires_at_epoch_us) == (3, 5_000_000)
    assert again.token != new.token


@pytest.mark.parametrize(
    "changes", [{"actor_id": "foreign"}, {"project_id": "foreign"}, {"operation_id": "foreign"}]
)
def test_foreign_actor_project_lookup_denied(journal, changes):
    store, _, root = _begin(journal)
    lease = store.acquire_lease(_context(), "op-1", 1)
    foreign = _context(**changes)
    for call in (
        lambda: store.mark_running(foreign, "op-1"),
        lambda: store.record_effect(foreign, "op-1", _effect()),
        lambda: store.finalize_operation(foreign, OutcomeReceipt.model_validate_json(_RECEIPT)),
        lambda: store.acquire_lease(foreign, "op-1", 1),
        lambda: store.renew_lease(foreign, lease, 1),
        lambda: store.release_lease(foreign, lease),
    ):
        with pytest.raises(ValueError, match="operation_denied") as caught:
            call()
        assert "a1" not in str(caught.value) and "p1" not in str(caught.value)
    assert _rows(root, "SELECT state FROM platform_operations") == [("prepared",)]
    if "operation_id" not in changes:
        result = store.begin_operation(foreign, _request())
        assert result.state == "idempotency_conflict"
        own = foreign.model_copy(update={"operation_id": "own"})
        assert store.begin_operation(own, _request()).state == "started"


def test_real_process_interrupt_leaves_recoverable_operation(tmp_path):
    child = _child(tmp_path, mode="interrupt")
    try:
        assert child.stdout.readline().strip() == "ready"
        child.stdin.write("go\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "durable"
        child.kill()
        child.communicate(timeout=5)
        assert child.returncode < 0
        assert _rows(tmp_path, "SELECT state,status,outcome_json FROM platform_operations") == [
            ("running", None, None)
        ]
        assert _rows(tmp_path, "SELECT state FROM platform_effects") == [("prepared",)]
        store = OperationStore(tmp_path, clock_epoch_us=lambda: 2_000_000)
        assert (
            store.begin_operation(_context(operation_id="retry"), _request()).state == "in_flight"
        )
        store.finalize_operation(
            _context(),
            OutcomeReceipt(
                operation_id="op-1",
                status="recovery_required",
                error_code="interrupted",
                action="Inspect the prepared effect.",
            ),
        )
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_canary_secret_absent_from_persisted_receipt(journal, caplog):
    store, _, root = journal
    canary = "request-secret-canary-7a9e"
    request = _request(payload={"password": canary, "nested": {"token": canary}})
    store.begin_operation(_context(), request)
    store.finalize_operation(_context(), OutcomeReceipt.model_validate_json(_RECEIPT))
    for table in ("platform_operations", "platform_effects", "platform_leases"):
        assert canary not in repr(_rows(root, f"SELECT * FROM {table}"))
    with sqlite3.connect(root / ".dango/dango.db") as conn:
        assert canary not in "\n".join(conn.iterdump())
        assert conn.execute("SELECT outcome_json FROM platform_operations").fetchone() == (
            _RECEIPT,
        )
    with pytest.raises(ValueError) as caught:
        store.mark_running(_context(operation_id=canary), canary)
    assert canary not in str(caught.value) and canary not in caplog.text
    assert all(
        canary.encode() not in path.read_bytes()
        for path in (root / ".dango").iterdir()
        if path.is_file()
    )


@pytest.mark.parametrize(
    "model,values",
    [
        (BeginResult, {"state": "started", "operation_id": "o"}),
        (EffectRecord, {"effect_id": "e", "sequence": 0, "name": "write", "state": "prepared"}),
        (
            LeaseBinding,
            {
                "project_id": "p",
                "operation_id": "o",
                "token": "t",
                "revision": 0,
                "expires_at_epoch_us": 0,
            },
        ),
    ],
)
def test_strict_records_required_fields_defaults_and_validation(model, values):
    record = model(**values)
    assert record.model_config == {
        "extra": "forbid",
        "frozen": True,
        "strict": True,
        "hide_input_in_errors": True,
    }
    for field in values:
        missing = {key: item for key, item in values.items() if key != field}
        for invalid in (
            {**values, field: None},
            missing,
            {**values, field: True},
            {**values, field: ""},
        ):
            with pytest.raises(ValidationError):
                model(**invalid)
    with pytest.raises(ValidationError):
        model(**values, unexpected="secret-canary")
    with pytest.raises(ValidationError):
        record.operation_id = "new"
    if model is BeginResult:
        assert record.receipt is None
    if model is EffectRecord:
        assert (record.native_ref, record.recovery_ref) == (None, None)
    for field in ("sequence", "revision", "expires_at_epoch_us"):
        if field in values:
            for bad in (-1, 1.0, "1", False):
                with pytest.raises(ValidationError):
                    model(**{**values, field: bad})


@pytest.mark.parametrize("ttl", [0, -1, True, False, 1.5, "1", None, 2**64])
def test_invalid_ttl_leaves_lease_unchanged(journal, ttl):
    store, _, root = _begin(journal)
    lease = store.acquire_lease(_context(), "op-1", 2)
    before = _rows(root, "SELECT * FROM platform_leases")
    for call in (
        lambda: store.acquire_lease(_context(), "op-1", ttl),
        lambda: store.renew_lease(_context(), lease, ttl),
    ):
        with pytest.raises(ValueError, match="invalid_input"):
            call()
    assert _rows(root, "SELECT * FROM platform_leases") == before


def test_null_keys_scoping_and_operation_id_collision(journal):
    store, _, root = _begin(journal)
    for i, changes in enumerate(({"actor_id": "a2"}, {"project_id": "p2"}, {})):
        ctx = _context(operation_id=f"independent-{i}", **changes)
        assert store.begin_operation(ctx, _request(idempotency_key=None)).state == "started"
    assert (
        store.begin_operation(
            _context(operation_id="another-name"), _request(operation_name="inspect")
        ).state
        == "started"
    )
    assert (
        store.begin_operation(_context(), _request(idempotency_key=None)).state
        == "idempotency_conflict"
    )
    assert _rows(root, "SELECT count(*) FROM platform_operations") == [(5,)]


@pytest.mark.parametrize("status", ["blocked", "failed", "cancelled", "recovery_required"])
def test_final_status_preserved(journal, status):
    store, _, root = _begin(journal)
    receipt = OutcomeReceipt(
        operation_id="op-1", status=status, error_code="safe_code", action="Inspect state."
    )
    assert store.finalize_operation(_context(), receipt) == receipt
    assert _rows(root, "SELECT state,status FROM platform_operations") == [("finalized", status)]


def test_stale_lease_bindings_and_finalized_cleanup(journal):
    store, _, _ = _begin(journal)
    lease = store.acquire_lease(_context(), "op-1", 2)
    for update in ({"token": "wrong"}, {"revision": 7}, {"expires_at_epoch_us": 99}):
        bad = lease.model_copy(update=update)
        for call in (
            lambda bad=bad: store.renew_lease(_context(), bad, 1),
            lambda bad=bad: store.release_lease(_context(), bad),
        ):
            with pytest.raises(ValueError, match="lease_conflict"):
                call()
    with pytest.raises(ValueError, match="lease_conflict"):
        store.acquire_lease(_context(), "op-1", 1)
    store.finalize_operation(_context(), OutcomeReceipt.model_validate_json(_RECEIPT))
    with pytest.raises(ValueError, match="operation_conflict"):
        store.renew_lease(_context(), lease, 1)
    store.release_lease(_context(), lease)


def test_transaction_failure_rolls_back_and_retry_succeeds(journal):
    store, _, root = _begin(journal)
    with sqlite3.connect(root / ".dango/dango.db") as conn:
        conn.execute(
            "CREATE TRIGGER reject_final BEFORE UPDATE ON platform_operations WHEN NEW.state='finalized' BEGIN SELECT RAISE(ABORT, 'private-database-canary'); END"
        )
    with pytest.raises(ValueError, match="store_unavailable") as caught:
        store.finalize_operation(_context(), OutcomeReceipt.model_validate_json(_RECEIPT))
    assert "private-database-canary" not in str(caught.value)
    assert _rows(root, "SELECT state,status,outcome_json FROM platform_operations") == [
        ("prepared", None, None)
    ]
    with sqlite3.connect(root / ".dango/dango.db") as conn:
        conn.execute("DROP TRIGGER reject_final")
    assert (
        store.finalize_operation(_context(), OutcomeReceipt.model_validate_json(_RECEIPT)).status
        == "success"
    )


@pytest.mark.parametrize("bad", [True, -1, 1.5, "1", None, 2**63])
def test_invalid_server_clock_and_forged_records_are_safe(journal, bad):
    store, now, root = _begin(journal)
    assert _rows(root, "SELECT created_at FROM platform_operations") == [
        ("1970-01-01T00:00:01.000000+00:00",)
    ]
    now[0] = bad
    with pytest.raises(ValueError, match="invalid_input"):
        store.mark_running(_context(), "op-1")
    now[0] = 1_000_000
    with pytest.raises(ValueError, match="invalid_input"):
        store.begin_operation(
            _context().model_copy(update={"operation_id": "bad\ud800"}),
            _request(idempotency_key=None),
        )
    for receipt in (
        None,
        OutcomeReceipt.model_construct(operation_id="op-1", status="secret-canary"),
    ):
        with pytest.raises(ValueError, match="invalid_input") as caught:
            store.finalize_operation(_context(), receipt)
        assert "secret-canary" not in str(caught.value)
    assert _rows(root, "SELECT state,status FROM platform_operations") == [("prepared", None)]


def test_concurrent_leases_admit_one(tmp_path):
    children = [_child(tmp_path, f"lease-{n}", "lease") for n in range(2)]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = [json.loads(child.communicate(timeout=20)[0]) for child in children]
        assert all(child.returncode == 0 for child in children)
        assert sum(item.get("code") == "lease_conflict" for item in results) == 1
        assert _rows(
            tmp_path, "SELECT count(*),revision,expires_at_epoch_us FROM platform_leases"
        ) == [(1, 0, 2_000_000)]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
