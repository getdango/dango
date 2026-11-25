"""tests/integration/native_workspaces.py

Run the exact-pin workspace join only through an explicit guarded-import or native CLI.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def check_imports(root: Path) -> None:
    """Exercise target initializer/bootstrap closure before any native service is released."""
    from unittest.mock import patch

    import duckdb

    assert sys.version_info[:3] == (3, 14, 10) and duckdb.__version__ == "2.0.0"
    with (
        patch("socket.socket", side_effect=AssertionError("Import network IO")),
        patch("sqlite3.connect", side_effect=AssertionError("Import SQLite IO")),
        patch("duckdb.connect", side_effect=AssertionError("Import native IO")),
    ):
        from dango.migrations.metadata.bootstrap import ensure_metadata_schema
        from dango.operations.contracts import OperationContext, OperationRequest
        from dango.operations.store import OperationStore
        from dango.storage.workspaces import WorkspaceService

        assert WorkspaceService.__module__ == "dango.storage.workspaces"
    root.mkdir(mode=0o700)
    store = OperationStore(root, clock_epoch_us=lambda: 1000000)
    context = OperationContext(
        operation_id="import",
        project_id="synthetic",
        actor_id="ordinary",
        capability="workspace",
        permission_revision="r1",
        trace_id="import",
    )
    result = store.begin_operation(context, OperationRequest(operation_name="import", payload={}))
    ensure_metadata_schema(root)
    with sqlite3.connect(root / ".dango/dango.db") as connection:
        assert connection.execute("SELECT state FROM platform_operations").fetchall() == [
            ("prepared",)
        ]
        assert connection.execute("SELECT version FROM _migrations").fetchall() == [(1,)]
    assert result.state == "started"
    (root.parent / "target-import.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "python": sys.version.split()[0],
                "duckdb": duckdb.__version__,
                "temporary_sqlite_bootstrap": True,
                "native_services_started": False,
            }
        )
    )


class WorkspaceRig:
    """Bind actual native allocation to admitted journal operations and a server clock."""

    def __init__(self, root: Path):
        from native_catalog_bindings import NativeRig

        from dango.operations.store import OperationStore
        from dango.storage.workspaces import WorkspaceService

        self.native = NativeRig(root)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.store = OperationStore(
            root, clock_epoch_us=lambda: int(self.now.timestamp() * 1000000)
        )
        self.service = WorkspaceService(root, self.native.allocator(), self.store, lambda: self.now)

    def admit(self, name, payload, revision=None, *, key=None):
        """Use the canonical admitted operation ID, never the proposed retry ID."""
        from dango.operations.contracts import OperationRequest

        context = self.native.context.model_copy(update={"operation_id": uuid4().hex})
        result = self.store.begin_operation(
            context,
            OperationRequest(
                operation_name=name,
                payload=payload,
                expected_revision=None if revision is None else str(revision),
                idempotency_key=key,
            ),
        )
        return context.model_copy(update={"operation_id": result.operation_id})

    def allocate(self, *, key=None):
        """Allocate only through the changed WorkspaceService/journal join."""
        context = self.admit(
            "workspace.allocate",
            {"branch_ref": "feature/same", "code_commit": "fixture-commit", "location": "remote"},
            key=key,
        )
        return context, self.service.allocate(context, "feature/same", "fixture-commit", "remote")

    def populate(self, context, workspace):
        """Populate independently known original-schema rows after service allocation."""
        binding = self.native.current(workspace.workspace_id)
        with self.native.allocator().attach(context, binding, read_only=False) as connection:
            connection.execute("CREATE SCHEMA catalog.sales; CREATE SCHEMA catalog.finance")
            connection.execute("CREATE TABLE catalog.sales.orders(id INTEGER, amount INTEGER)")
            connection.execute("INSERT INTO catalog.sales.orders VALUES (1,100),(2,200),(3,50)")
            connection.execute("CREATE TABLE catalog.finance.orders AS SELECT 7::INTEGER amount")
        return binding

    def expire(self, workspace):
        """Admit an explicit eligible expiry with the exact current revision."""
        context = self.admit(
            "workspace.expire", {"workspace_id": workspace.workspace_id}, workspace.revision
        )
        return self.service.expire(context, workspace.workspace_id, workspace.revision)

    def peer(self):
        """Use another actual ordinary role with shared project/workspace state roots."""
        from dango.storage.workspaces import WorkspaceService

        peer = copy.copy(self)
        peer.native = copy.copy(self.native)
        peer.native.context = self.native.context.model_copy(update={"actor_id": "task-peer"})
        peer.native.ref = self.native.ref.model_copy(update={"actor_id": "task-peer"})
        peer.native.credential = json.loads(Path("/secrets/catalog-b.json").read_text())
        peer.native.control = {"grant": True, "current": peer.native.ref, "resolves": 0}
        peer.service = WorkspaceService(
            self.native.root, peer.native.allocator(), peer.store, lambda: peer.now
        )
        return peer

    def rows(self, sql, args=()):
        """Read journal facts through an independent SQLite connection."""
        with sqlite3.connect(self.native.root / ".dango/dango.db") as connection:
            return connection.execute(sql, args).fetchall()

    def manifest(self, workspace_id):
        """Locate the durable service record independently of its returned Workspace."""
        for path in (self.native.root / ".dango/state/workspaces/resources").glob("*.json"):
            value = json.loads(path.read_text())
            if value["workspace_id"] == workspace_id:
                return path, value
        raise AssertionError("Missing workspace record")

    def finish(self, context):
        """Finalize a synthetic consumer after its actual use/release has ended."""
        from dango.operations.contracts import OutcomeReceipt

        self.store.finalize_operation(
            context, OutcomeReceipt(operation_id=context.operation_id, status="success")
        )

    def dispose(self, workspace):
        """Explicitly expire an eligible fixture and independently require physical/native absence."""
        from dango.storage.workspaces import Workspace

        _, value = self.manifest(workspace.workspace_id)
        current = Workspace.model_validate_json(json.dumps(value["workspace"]))
        self.now = max(self.now, datetime.fromisoformat(current.lease_expiry)) + timedelta(
            seconds=1
        )
        receipt = self.expire(current)
        assert receipt.status == "success"
        binding = self.native.current(current.workspace_id)
        assert not Path(binding.data_root_ref).exists()
        assert not self.native.sql(
            f"SELECT oid FROM pg_namespace WHERE nspname='{binding.metadata_schema}'"
        )
        return receipt


def child(action, expected=0, *, wait=True):
    """Bound one task-owned Linux process; no connection or SQLite transaction is open at fork."""
    pid = os.fork()
    if pid == 0:
        try:
            action()
        except BaseException:
            os._exit(91)
        os._exit(0)
    if not wait:
        return pid
    wait_child(pid, expected)
    return None


def wait_child(pid, expected=0):
    """Wait only for a known task-owned child and kill that child on its bounded deadline."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        found, status = os.waitpid(pid, os.WNOHANG)
        if found:
            assert os.waitstatus_to_exitcode(status) == expected
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    raise AssertionError("Owned child deadline exceeded")


def inspect_owned(rig):
    """02: inspect another owned operation and compare exact independent SQL values."""
    from dango.operations.store import EffectRecord

    target, caller = rig.admit("target", {}), rig.admit("caller", {})
    assert rig.store.inspect_effect(caller, target.operation_id, "capture").effect is None
    effect = EffectRecord(
        effect_id="capture", sequence=0, name="capture", state="prepared", native_ref="captured"
    )
    rig.store.record_effect(target, target.operation_id, effect)
    observed = rig.store.inspect_effect(caller, target.operation_id, "capture")
    assert (
        tuple(observed.effect.model_dump().values())
        == rig.rows(
            "SELECT effect_id,effect_sequence,effect_name,state,native_ref,recovery_ref FROM platform_effects WHERE operation_id=?",
            (target.operation_id,),
        )[0]
    )
    rig.finish(target)
    assert (
        rig.store.inspect_effect(caller, target.operation_id, "capture").receipt.status == "success"
    )
    rig.finish(caller)
    return {"classification": "target_sqlite", "exact_effect": True}


def inspect_negatives(rig):
    """03: preserve writes and refuse corrupt or cross-owner journal evidence."""
    target, caller = rig.admit("target", {}), rig.admit("caller", {})
    try:
        rig.store.mark_running(caller, target.operation_id)
    except ValueError as error:
        assert error.code == "operation_denied"
    else:
        raise AssertionError("Cross-operation write admitted")
    rig.finish(target)
    with sqlite3.connect(rig.native.root / ".dango/dango.db") as connection:
        connection.execute(
            "UPDATE platform_operations SET result_ref='inconsistent' WHERE operation_id=?",
            (target.operation_id,),
        )
    try:
        rig.store.inspect_effect(caller, target.operation_id, "capture")
    except ValueError as error:
        assert error.code == "store_unavailable"
    else:
        raise AssertionError("Inconsistent receipt admitted")
    rig.finish(caller)
    return {"classification": "target_sqlite", "corruption_denied": True}


def denied(action):
    """Require a fixed safe refusal, never turn an arbitrary exception into a passing denial."""
    from dango.storage.workspaces import WorkspaceError

    try:
        action()
    except WorkspaceError as error:
        assert "password" not in str(error) and "host=" not in str(error)
        return error.code
    raise AssertionError("Expected workspace denial")


def expiry_context(rig, workspace):
    """Bind an exact explicit expiry input independently of the service return."""
    rig.now = max(rig.now, datetime.fromisoformat(workspace.lease_expiry)) + timedelta(seconds=1)
    return rig.admit(
        "workspace.expire", {"workspace_id": workspace.workspace_id}, workspace.revision
    )


def pin_expiry_race(rig):
    """Require a racing pin either to retain actual data or to refuse an expired workspace."""
    from dango.storage.workspaces import WorkspaceError

    owner, workspace = rig.allocate()
    binding = rig.populate(owner, workspace)
    expiry = expiry_context(rig, workspace)
    consumer = rig.admit("query", {})
    gate, pin_path, expiry_path = [
        rig.native.root / name for name in ("race-go", "race-pin", "race-expiry")
    ]

    def wait_gate():
        while not gate.exists():
            time.sleep(0.01)

    def acquire():
        wait_gate()
        try:
            pin = rig.service.acquire_pin(consumer, workspace.workspace_id, "query", "result")
            value = {"status": "pinned", "pin_id": pin.pin_id}
        except WorkspaceError as error:
            assert error.code == "workspace_conflict"
            value = {"status": "refused"}
        pin_path.write_text(json.dumps(value))

    def expire():
        wait_gate()
        value = rig.service.expire(expiry, workspace.workspace_id, 0)
        expiry_path.write_text(value.model_dump_json())

    pids = [child(acquire, wait=False), child(expire, wait=False)]
    gate.touch(mode=0o600)
    for pid in pids:
        wait_child(pid)
    pin_result = json.loads(pin_path.read_text())
    receipt = json.loads(expiry_path.read_text())
    if pin_result["status"] == "pinned":
        assert receipt["status"] == "blocked" and receipt["error_code"] == "workspace_retained"
        with rig.native.allocator().attach(owner, binding, read_only=True) as connection:
            assert connection.execute(
                "SELECT sum(amount) FROM catalog.sales.orders"
            ).fetchone() == (350,)
        rig.service.release_pin(consumer, workspace.workspace_id, pin_result["pin_id"])
        rig.finish(consumer)
        rig.dispose(workspace)
    else:
        assert receipt["status"] == "success" and not Path(binding.data_root_ref).exists()
        rig.finish(consumer)
    return {"pin": pin_result["status"], "expiry": receipt["status"]}


def smoke(rig: WorkspaceRig) -> dict:
    """Prove populated allocate, durable reopen and actual owned expiry before expansion."""
    from native_catalog_bindings import (
        test_read_only_prod_binding_refuses_write,
        test_same_table_in_two_original_schemas,
    )

    from dango.storage.workspaces import WorkspaceService

    role = rig.native.sql(
        "SELECT current_user,session_user,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname=current_user"
    )
    user = rig.native.credential["user"]
    assert role == [(user, user, False, False, False, False, False, True)]
    context, workspace = rig.allocate()
    binding = rig.populate(context, workspace)
    before = rig.native.metadata(binding)
    assert dict(before["metadata"])["version"] == "1.1"
    assert [(r[0], r[1]) for r in before["tables"]] == [("finance", "orders"), ("sales", "orders")]
    reopened = WorkspaceService(rig.native.root, rig.native.allocator(), rig.store, lambda: rig.now)
    assert reopened.get(context, workspace.workspace_id) == workspace
    with rig.native.allocator().attach(context, binding, read_only=True) as connection:
        actual = test_same_table_in_two_original_schemas(connection)
        denial = test_read_only_prod_binding_refuses_write(connection)
    assert rig.native.metadata(binding) == before
    rig.now += timedelta(days=8)
    receipt = rig.expire(workspace)
    assert receipt.status == "success" and receipt.resource_revision == "2"
    assert not Path(binding.data_root_ref).exists()
    assert not rig.native.sql(
        f"SELECT oid FROM pg_namespace WHERE nspname='{binding.metadata_schema}'"
    )
    assert reopened.get(context, workspace.workspace_id).state == "expired"
    return {
        "actual": actual,
        "ordinary_role": role,
        "write_denial_type": denial,
        "metadata_stable": True,
        "owned_root_absent": True,
        "owned_namespace_absent": True,
    }


def main() -> None:
    """Retain separate smoke/finite statuses; imports alone start no service or network."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-imports", type=Path)
    parser.add_argument("--task-root", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--phase", choices=["smoke", "all"], default="smoke")
    args = parser.parse_args()
    if args.check_imports:
        check_imports(args.check_imports)
        return
    assert args.task_root and args.commit
    report = {
        "status": "FAIL",
        "classification": "integration",
        "commit": args.commit,
        "phase": args.phase,
        "cases": {f"{n:02d}": "UNRUN" for n in range(1, 19)},
    }
    output = args.task_root.parent / "native-result.json"
    try:
        rig = WorkspaceRig(args.task_root)
        from native_catalog_bindings import fixture_bytes

        report["pins"] = {
            "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "python": sys.version.split()[0],
            "duckdb": sys.modules["duckdb"].__version__,
            "ducklake_metadata_version": "1.1",
            "qualification": "development_only",
        }
        report["cases"]["18"] = "FAIL"
        report["observations"] = smoke(rig)
        report["cases"]["18"] = "PASS"
        report.update(status="PASS_NATIVE_WORKSPACE_SMOKE", owned_cleanup=True)
        (args.task_root.parent / "smoke-result.json").write_text(json.dumps(report) + "\n")
        if args.phase == "all":
            from workspace_cases import CASES

            for number, case in CASES:
                report["case_stage"] = number
                report["cases"][number] = "FAIL"
                output.write_text(json.dumps(report) + "\n")
                (args.task_root.parent / "native-stage.txt").write_text(number)
                observations = case(rig)
                report.setdefault("case_observations", {})[number] = observations
                report["cases"][number] = "PASS"
                output.write_text(json.dumps(report) + "\n")
            assert all(state == "PASS" for state in report["cases"].values())
            report["status"] = "PASS_NATIVE_WORKSPACE_MATRIX"
    except Exception as error:
        report.update(status="FAIL", error_type=type(error).__name__)
        raise SystemExit("Workspace integration failed; inspect sanitized case report") from None
    finally:
        output.write_text(json.dumps(report) + "\n")


if __name__ == "__main__":
    main()
