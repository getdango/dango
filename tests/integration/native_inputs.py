"""tests/integration/native_inputs.py

Run populated captured-input proof only through the explicit exact-profile task entrypoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from uuid import uuid4

from native_workspaces import WorkspaceRig


def imports(root: Path) -> None:
    """Prove capture import performs no network/native/SQLite IO before service bootstrap."""
    from unittest.mock import patch

    from native_workspaces import check_imports

    with (
        patch("socket.socket", side_effect=AssertionError("Import network IO")),
        patch("sqlite3.connect", side_effect=AssertionError("Import SQLite IO")),
        patch("duckdb.connect", side_effect=AssertionError("Import native IO")),
    ):
        from dango.storage.inputs import InputCaptureService

        assert InputCaptureService.__module__ == "dango.storage.inputs"
    check_imports(root)
    path = root.parent / "target-import.json"
    value = json.loads(path.read_text())
    value["capture_import_guarded"] = True
    path.write_text(json.dumps(value))


class InputRig(WorkspaceRig):
    """Seal one controller-guarded synthetic fixture; no general source registry or retention service."""

    def __init__(self, root: Path):
        super().__init__(root)
        self.source_id = None
        self.producer_id = None
        self.teardown = False
        allocator = self.native.allocator()
        previous = allocator.authorize
        allocator.authorize = lambda ctx, binding, action: (
            previous(ctx, binding, action)
            and (
                binding.workspace_id != self.source_id
                or action == "read"
                or self.teardown
                or (action == "write" and ctx.operation_id == self.producer_id)
            )
        )
        self.allocator = allocator
        self.service.allocator = allocator
        from dango.storage.inputs import InputCaptureService

        self.inputs = InputCaptureService(root, allocator, self.service, lambda: self.now)
        self.captures = []
        self.targets = []

    def install(self):
        """Populate 350/7 and freeze its actual snapshot/UUIDs before waiting on the controller lock."""
        from dango.storage.input_state import (
            AcceptedFixture,
            Column,
            Guardian,
            Relation,
            directory,
            fingerprint,
        )
        from dango.storage.workspace_state import LockedRecord

        owner, self.source = self.allocate()
        self.source_context = owner
        binding = self.native.current(self.source.workspace_id)
        with self.allocator.attach(owner, binding, read_only=False) as connection:
            connection.execute("CREATE SCHEMA catalog.sales; CREATE SCHEMA catalog.finance")
            connection.execute("CREATE TABLE catalog.sales.orders(id INTEGER, amount INTEGER)")
            connection.execute("INSERT INTO catalog.sales.orders VALUES (1,100),(2,200),(3,50)")
            connection.execute("CREATE TABLE catalog.finance.orders(amount BIGINT)")
            connection.execute("INSERT INTO catalog.finance.orders VALUES (7)")
        metadata = self.native.metadata(binding)
        self.snapshot = metadata["snapshots"][-1][0]
        ids = {r[0]: r[2] for r in metadata["tables"]}
        specs = {
            "sales": (
                Column(name="id", type="INTEGER", nullable=True),
                Column(name="amount", type="INTEGER", nullable=True),
            ),
            "finance": (Column(name="amount", type="BIGINT", nullable=True),),
        }
        self.manifest = AcceptedFixture(
            kind="accepted-input-fixture-v1",
            fixture_id="p0-selected-inputs-v1",
            accepted_release_id="p0-accepted-v1",
            project_id=owner.project_id,
            actor_id=owner.actor_id,
            catalog_id="accepted-production",
            binding=binding,
            snapshot_id=self.snapshot,
            relations=tuple(
                Relation(
                    relation_id=name,
                    schema_name=name,
                    table_name="orders",
                    table_uuid=ids[name],
                    columns=columns,
                    schema_fingerprint=fingerprint(columns),
                    source_version_ref="source-v1-" + name,
                    checkpoint_ref="checkpoint-v1-" + name,
                )
                for name, columns in specs.items()
            ),
            retention_mode="exclusive_fixture_lifetime",
            maintenance_mode="disabled",
            lifetime_nonce=uuid4().hex,
        )
        fd = directory(self.native.root)
        try:
            LockedRecord(fd, "accepted").save(self.manifest)
            guard = os.open("lifetime.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            try:
                value = Guardian(
                    kind="input-fixture-lifetime-v1",
                    nonce=self.manifest.lifetime_nonce,
                    project_id=owner.project_id,
                    actor_id=owner.actor_id,
                    source_workspace_id=self.source.workspace_id,
                    source_binding_id=binding.binding_id,
                    manifest_sha256=hashlib.sha256(
                        self.manifest.model_dump_json().encode()
                    ).hexdigest(),
                )
                os.write(guard, value.model_dump_json().encode())
                os.fsync(guard)
            finally:
                os.close(guard)
        finally:
            os.close(fd)
        self.source_id = self.source.workspace_id
        deadline = time.monotonic() + 60
        while not (self.native.root.parent / "lifetime-held").exists():
            assert time.monotonic() < deadline, "Controller lifetime lock not established"
            time.sleep(0.05)
        from dango.storage.input_state import fixture

        fixture(self.native.root, owner)
        self.producer_id = self.admit("fixture.advance", {}).operation_id
        producer = owner.model_copy(update={"operation_id": self.producer_id})
        with self.allocator.attach(producer, binding, read_only=False) as connection:
            connection.execute("UPDATE catalog.sales.orders SET amount=60 WHERE id=3")
            assert connection.execute(
                "SELECT sum(amount) FROM catalog.sales.orders"
            ).fetchone() == (360,)
        self.finish(producer)
        self.producer_id = None
        self.ready_metadata = self.native.metadata(binding)

    def capture(self, *, relations=("sales",), maximum=1000000, context=None, target=None):
        """Admit exact capture arguments and retain original contexts for owned cleanup."""
        if target is None:
            _, target = self.allocate()
            self.targets.append(target)
        payload = {
            "workspace_id": target.workspace_id,
            "accepted_release_id": "p0-accepted-v1",
            "relation_ids": list(relations),
            "max_bytes": maximum,
        }
        context = context or self.admit("inputs.capture", payload)
        try:
            result = self.inputs.capture_inputs(
                context, target.workspace_id, "p0-accepted-v1", relations, maximum
            )
        finally:
            rows = self.rows(
                "SELECT native_ref FROM platform_effects WHERE operation_id=? AND effect_name='inputs.capture'",
                (context.operation_id,),
            )
            if rows and (context, rows[0][0]) not in self.captures:
                self.captures.append((context, rows[0][0]))
        return context, result

    def release(self):
        """End fixture lifetime only after all capture pins and explicitly disposed workspaces are gone."""
        for context, identity in self.captures:
            self.inputs.release_capture(context, identity)
        self.teardown = True
        for workspace in self.targets:
            self.dispose(workspace)
        self.dispose(self.source)
        assert not list((self.native.root / "data").iterdir())
        assert not self.native.sql("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'dango_%'")
        (self.native.root.parent / "lifetime-released").touch(mode=0o600)


def smoke(rig):
    """Capture old-S350 after current360; reopen, prove native read-only guards and remove owned data."""
    from dango.storage.catalogs import CatalogError
    from dango.storage.inputs import InputCaptureService

    role = rig.native.sql(
        "SELECT current_user,session_user,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname=current_user"
    )
    user = rig.native.credential["user"]
    assert role == [(user, user, False, False, False, False, False, True)]
    peer = rig.peer()
    peer_user = peer.native.credential["user"]
    assert peer.native.sql(
        "SELECT current_user,session_user,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname=current_user"
    ) == [(peer_user, peer_user, False, False, False, False, False, True)]
    _, peer_workspace = peer.allocate()
    peer.dispose(peer_workspace)
    rig.install()
    context, result = rig.capture()
    assert result.state == "ready" and result.estimated_bytes > 0 and result.captured_bytes > 0
    assert result.catalog_snapshot_ids == {"accepted-production": str(rig.snapshot)}
    target = rig.native.current(result.workspace_id)
    with rig.allocator.attach(context, target, read_only=True) as connection:
        assert connection.execute("SELECT * FROM catalog.sales.orders ORDER BY id").fetchall() == [
            (1, 100),
            (2, 200),
            (3, 50),
        ]
        assert connection.execute("SELECT sum(amount) FROM catalog.sales.orders").fetchone() == (
            350,
        )
        assert connection.execute(
            "SELECT table_schema,table_name FROM information_schema.tables WHERE table_catalog='catalog' ORDER BY 1,2"
        ).fetchall() == [("sales", "orders")]
    reopened = InputCaptureService(rig.native.root, rig.allocator, rig.service, lambda: rig.now)
    assert reopened.inspect_capture(context, result.input_set_id) == result
    try:
        with rig.allocator.attach(context, rig.manifest.binding, read_only=False):
            raise AssertionError("Sealed source write admitted")
    except CatalogError:
        pass
    maintenance = (
        "CALL ducklake_expire_snapshots('catalog', older_than => now() + INTERVAL '1 day')"
    )
    with rig.allocator.attach(context, rig.manifest.binding, read_only=True) as connection:
        functions = connection.execute(
            "SELECT function_name FROM duckdb_functions() WHERE function_name='ducklake_expire_snapshots'"
        ).fetchall()
        assert functions, "Actual maintenance function must exist"
        for sql in ("UPDATE catalog.sales.orders SET amount=999 WHERE id=1", maintenance):
            try:
                connection.execute(sql)
            except Exception as error:
                assert "read-only" in str(error).lower() or "read only" in str(error).lower()
            else:
                raise AssertionError("Native read-only maintenance/write admitted")
    assert rig.native.metadata(rig.manifest.binding) == rig.ready_metadata
    observed = {
        "capture_sum": 350,
        "current_sum": 360,
        "ordinary_role": role,
        "estimated_bytes": result.estimated_bytes,
        "captured_bytes": result.captured_bytes,
        "read_only_maintenance_denied": True,
        "old_snapshot_after_advance": True,
    }
    return observed


def main():
    """Keep smoke separate from the fixed12 matrix, which remains UNRUN until implemented/executed."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-imports", type=Path)
    parser.add_argument("--task-root", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--phase", choices=["smoke", "all"], default="smoke")
    args = parser.parse_args()
    if args.check_imports:
        imports(args.check_imports)
        return
    assert args.task_root and args.commit
    report = {
        "status": "FAIL",
        "commit": args.commit,
        "phase": args.phase,
        "classification": "integration",
        "cases": {f"{n:02d}": "UNRUN" for n in range(1, 13)},
        "smoke": "FAIL",
    }
    output = args.task_root.parent / "native-result.json"
    try:
        rig = InputRig(args.task_root)
        from native_catalog_bindings import fixture_bytes

        report["profile"] = {
            "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "python": sys.version.split()[0],
            "duckdb": sys.modules["duckdb"].__version__,
            "ducklake": "1.1",
            "qualification": "development_only",
        }
        report["observations"] = smoke(rig)
        report["smoke"] = "PASS"
        if args.phase == "all":
            raise AssertionError(
                "Finite matrix expansion requires an earlier meaningful smoke PASS"
            )
        rig.release()
        report.update(
            status="PASS_NATIVE_INPUT_SMOKE",
            owned_cleanup=True,
            lifetime_released_after_all_refs=True,
        )
    except Exception as error:
        report.update(
            status="FAIL",
            error_type=type(error).__name__,
            safe_code=getattr(error, "code", None),
            failure_frames=[
                {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(error.__traceback__)
            ],
        )
        raise SystemExit(
            "Input integration failed; inspect sanitized stage/case evidence"
        ) from None
    finally:
        output.write_text(json.dumps(report) + "\n")
        (args.task_root.parent / "smoke-result.json").write_text(json.dumps(report) + "\n")


if __name__ == "__main__":
    main()
