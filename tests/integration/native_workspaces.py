"""tests/integration/native_workspaces.py

Run the exact-pin workspace join only through an explicit guarded-import or native CLI.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
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
        report["cases"]["18"] = "FAIL"
        report["observations"] = smoke(rig)
        report["cases"]["18"] = "PASS"
        report.update(status="PASS_NATIVE_WORKSPACE_SMOKE", owned_cleanup=True)
        (args.task_root.parent / "smoke-result.json").write_text(json.dumps(report) + "\n")
        if args.phase == "all":
            raise RuntimeError("Finite matrix expansion follows the smoke milestone")
    except Exception as error:
        report.update(status="FAIL", error_type=type(error).__name__)
        raise SystemExit("Workspace integration failed; inspect sanitized case report") from None
    finally:
        output.write_text(json.dumps(report) + "\n")


if __name__ == "__main__":
    main()
