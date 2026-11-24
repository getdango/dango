"""tests/integration/native_catalog_bindings.py

Exact-pin ordinary-role smoke and finite integration matrix; import/collection performs no native IO.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

# Explicit integration CLI: no pytest dependency and no native/network IO during import.
FIXTURE = Path(__file__).parents[1] / "fixtures/runtime_profiles.json"
fixture_bytes = FIXTURE.read_bytes()
assert (
    hashlib.sha256(fixture_bytes).hexdigest()
    == "b6879d50f7220b8afa1a1b6b6266e27db6c3492a8fa2bc71747656c6411af449"
)
COMPONENTS = {r["component"]["component_id"]: r for r in json.loads(fixture_bytes)["artifacts"]}


ARTIFACTS = []
for name, size in {
    "duckdb-2.0.0-cp312-abi3-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl": 24371005,
    "typing_extensions-4.16.0-py3-none-any.whl": 45571,
    "cpython-3.14.10+20261026-x86_64-unknown-linux-gnu-install_only.tar.gz": 76564514,
    "pyyaml-6.0.3-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl": 794175,
}.items():
    component = COMPONENTS["t44a-" + name]["component"]
    ARTIFACTS.append((name, component["artifact_ref"], component["sha256"], size))
EXTENSIONS = {}
for name, compressed, expanded in [
    ("ducklake", 17217388, 54882304),
    ("postgres_scanner", 18910802, 59224064),
]:
    row = COMPONENTS["t44a-extension-" + name]
    EXTENSIONS[name] = {
        "url": row["component"]["artifact_ref"],
        "sha256": row["component"]["sha256"],
        "expanded_sha256": row["native_hashes"]["expanded"],
        "compressed": compressed,
        "expanded_upper": expanded,
    }


def disk_gate(root: Path, remaining: int = 0) -> None:
    """Reserve finite remaining allocations and measured two-GiB headroom in task storage."""
    assert root.is_mount(), "Owned bounded filesystem is required"
    free = shutil.disk_usage(root).free
    assert free >= 2147483648 + remaining, "Task filesystem headroom/reservation failed"


def download(root, name, url, digest, size):
    """Fetch frozen public bytes with identifying UA and a bounded, sanitized stage."""
    (root / "work/acquisition-stage.json").write_text(
        json.dumps({"artifact": name, "phase": "http_get"})
    )
    print("acquire:", name, flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Dango-T04A-Qualification/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read(size + 1)
    assert len(payload) == size and hashlib.sha256(payload).hexdigest() == digest
    return payload


def acquire(root: Path) -> None:
    """Acquire only frozen minimal artifacts; host controller is already slice-constrained."""
    disk_gate(root, 2 * 1024**3)
    tools = root / "tools"
    artifacts = tools / "artifacts"
    artifacts.mkdir(parents=True)
    wheel_upper = 0
    imports = (root / "import-pins.json").read_bytes()
    assert (
        hashlib.sha256(imports).hexdigest()
        == "6ec55230a611aacd94f19b39ad0dfd615cdeb995f11833d7e7a984856286f7fd"
    )
    for filename, url, digest, size in ARTIFACTS + json.loads(imports):
        disk_gate(root, size + 1024**3)
        target = artifacts / filename
        with target.open("xb") as output:
            output.write(download(root, filename, url, digest, size))
        if filename.endswith(".tar.gz"):
            with tarfile.open(target) as archive:
                members = archive.getmembers()
                upper = sum(((m.size + 4095) // 4096 + 1) * 4096 for m in members)
                assert upper <= 299630592, "Frozen interpreter extraction bound"
                disk_gate(root, upper + 1024**3)
                archive.extractall(tools, filter="data")
            binary = tools / "python/bin/python3.14"
            assert (
                hashlib.sha256(binary.read_bytes()).hexdigest()
                == "a3c5f01bf9e5cd625b74fd700bb58a579c82df228c50b57d2485d006582a8b01"
            )
        else:
            with zipfile.ZipFile(target) as archive:
                wheel_upper += sum(
                    ((m.file_size + 4095) // 4096 + 1) * 4096 for m in archive.infolist()
                )
            assert wheel_upper <= 160 * 1024**2, "Offline wheel installation bound"
    extensions = tools / "extensions"
    extensions.mkdir()
    for name, pin in EXTENSIONS.items():
        disk_gate(root, pin["compressed"] + pin["expanded_upper"] + 1024**3)
        compressed = download(root, name, pin["url"], pin["sha256"], pin["compressed"])
        expanded = gzip.decompress(compressed)
        assert (
            len(expanded) <= pin["expanded_upper"]
            and hashlib.sha256(expanded).hexdigest() == pin["expanded_sha256"]
        )
        (extensions / (name + ".duckdb_extension")).write_bytes(expanded)
    receipt = {
        "status": "PASS",
        "artifacts": [r[0] for r in ARTIFACTS + json.loads(imports)],
        "wheel_allocated_upper": wheel_upper,
    }
    (root / "work/acquisition.json").write_text(json.dumps(receipt))


class NativeRig:
    """Use the actual adapter and ordinary role; hooks interrupt durable boundaries only."""

    def __init__(self, root: Path):
        from dango.operations.contracts import OperationContext
        from dango.security.native_credentials import SecretRef
        from dango.storage import catalogs

        self.root = root
        self.catalogs = catalogs
        root.mkdir(mode=0o700)
        for name in ("state", "data"):
            (root / name).mkdir(mode=0o700)
        self.credential = json.loads(Path("/secrets/catalog.json").read_text())
        self.ref = SecretRef(
            project_id="task-project", actor_id="task-owner", credential_id="ordinary", version="v1"
        )
        self.context = OperationContext(
            operation_id="native",
            project_id="task-project",
            actor_id="task-owner",
            capability="catalog.admin",
            permission_revision="current",
            trace_id="synthetic",
        )
        self.port = catalogs.NativeCatalogPort(Path("/tools/extensions"))
        self.control = {"grant": True, "current": self.ref, "resolves": 0}

    def allocator(self):
        """Construct a fresh facade over current persisted state and server-owned authority."""

        def resolve(ref):
            self.control["resolves"] += 1
            return dict(self.credential) if ref == self.control["current"] else None

        return self.catalogs.CatalogAllocator(
            self.root / "state",
            self.root / "data",
            port=self.port,
            credential_ref=self.ref,
            catalog_database="t04a_catalog",
            bundle_id="t44a-python314-duckdb2-linux-amd64-development",
            resolve=resolve,
            authorize=lambda ctx, binding, action: (
                self.control["grant"] and ctx.permission_revision == "current"
            ),
        )

    def sql(self, sql, *, execute=False):
        """Use an independent raw PostgreSQL connection for the oracle or test-owned DDL."""
        with self.port._postgres(self.credential) as connection:
            method = self.port._execute if execute else self.port._query
            return method(connection, sql)

    def current(self, workspace):
        """Read the current binding from its durable private record, independent of allocator return."""
        records = [json.loads(p.read_text()) for p in (self.root / "state").glob("*.json")]
        return self.catalogs.CatalogBinding.model_validate(
            next(r["binding"] for r in records if r["binding"]["workspace_id"] == workspace)
        )

    def populate(self, workspace):
        """Create original sales/finance schemas and independently known fixture rows."""
        binding = self.allocator().allocate(self.context, workspace)
        with self.allocator().attach(self.context, binding, read_only=False) as connection:
            connection.execute("CREATE SCHEMA catalog.sales; CREATE SCHEMA catalog.finance")
            connection.execute("CREATE TABLE catalog.sales.orders(id INTEGER, amount INTEGER)")
            connection.execute("INSERT INTO catalog.sales.orders VALUES (1,100),(2,200),(3,50)")
            connection.execute("CREATE TABLE catalog.finance.orders AS SELECT 7::INTEGER amount")
        return binding

    def metadata(self, binding):
        """Observe raw namespace/spec/snapshot/table UUIDs without adapter evidence."""
        name = binding.metadata_schema
        schema = '"' + name + '"'
        queries = {
            "namespace": f"SELECT oid::bigint,obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname='{name}'",
            "metadata": f"SELECT key,value FROM {schema}.ducklake_metadata WHERE scope IS NULL ORDER BY key,value",
            "snapshots": f"SELECT snapshot_id FROM {schema}.ducklake_snapshot ORDER BY snapshot_id",
            "tables": f"SELECT s.schema_name,t.table_name,t.table_uuid::text FROM {schema}.ducklake_table t JOIN {schema}.ducklake_schema s USING(schema_id) WHERE t.end_snapshot IS NULL AND s.end_snapshot IS NULL ORDER BY s.schema_name,t.table_name",
            "spec": f"SELECT c.oid::bigint,c.relname,a.attnum,a.attname,a.atttypid::bigint FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid WHERE n.nspname='{name}' AND a.attnum>0 ORDER BY c.relname,a.attnum",
        }
        return {key: self.sql(query) for key, query in queries.items()}

    def removed(self, binding):
        """Require populated native and physical absence and an idempotent tombstone."""
        self.allocator().remove_owned(self.context, self.current(binding.workspace_id))
        assert not Path(binding.data_root_ref).exists()
        name = binding.metadata_schema
        assert not self.sql(f"SELECT oid FROM pg_namespace WHERE nspname='{name}'")
        self.allocator().remove_owned(self.context, self.current(binding.workspace_id))

    def denied(self, operation, *args):
        """Require the public safe boundary, retaining no underlying exception text."""
        try:
            operation(*args)
        except self.catalogs.CatalogError as error:
            assert error.__suppress_context__ and str(error).startswith("catalog_operation_denied:")
            assert self.credential["password"] not in str(error)
            return
        raise AssertionError("Catalog operation must be denied")


def test_same_table_in_two_original_schemas(connection) -> dict:
    """Execute independent fixture arithmetic and original-schema row checks."""
    rows = connection.execute("SELECT * FROM catalog.sales.orders ORDER BY id").fetchall()
    assert rows == [(1, 100), (2, 200), (3, 50)]
    assert connection.execute("SELECT sum(amount) FROM catalog.sales.orders").fetchone() == (350,)
    finance = connection.execute("SELECT * FROM catalog.finance.orders").fetchall()
    assert finance == [(7,)]
    return {"sales": sum(row[1] for row in rows), "finance": finance[0][0]}


def test_read_only_prod_binding_refuses_write(connection) -> str:
    """Require an actual engine read-only UPDATE rejection, not a bootstrap failure."""
    import duckdb

    try:
        connection.execute("UPDATE catalog.sales.orders SET amount=999 WHERE id=1")
    except duckdb.Error as error:
        assert "read-only" in str(error).lower() or "read only" in str(error).lower()
        return type(error).__name__
    raise AssertionError("Native UPDATE must be denied")


def test_native_prepare_and_reopen_no_schema_mutation(rig) -> None:
    """First target: populated prepare, durable RO reopen/write denial, owned cleanup."""
    import duckdb

    assert sys.version_info[:3] == (3, 14, 10) and duckdb.__version__ == "2.0.0"
    role = rig.sql(
        "SELECT current_user,session_user,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname=current_user"
    )
    assert role == [("t04a_writer", "t04a_writer", False, False, False, False, False, True)]
    binding = rig.populate("production")
    before = rig.metadata(binding)
    rig.observations = {"smoke_metadata": before, "ordinary_role": role}
    assert [(r[0], r[1]) for r in before["tables"]] == [("finance", "orders"), ("sales", "orders")]
    assert rig.allocator().allocate(rig.context, "production") == binding
    with rig.allocator().attach(rig.context, binding, read_only=True) as connection:
        rig.observations["actual"] = test_same_table_in_two_original_schemas(connection)
        rig.observations["write_denial_type"] = test_read_only_prod_binding_refuses_write(
            connection
        )
    assert rig.metadata(binding) == before, "Read-only reopen mutated native metadata"
    rig.removed(binding)


def test_distinct_catalog_and_data_roots(rig) -> None:
    """Require distinct namespaces, roots, physical identities and original table UUIDs."""
    one, two = rig.populate("one"), rig.populate("two")
    assert one.metadata_schema != two.metadata_schema and one.data_root_ref != two.data_root_ref
    assert Path(one.data_root_ref).stat().st_ino != Path(two.data_root_ref).stat().st_ino
    left, right = rig.metadata(one), rig.metadata(two)
    rig.observations["distinct_workspaces"] = [left, right]
    assert left["namespace"][0][0] != right["namespace"][0][0]
    assert len({r[2] for r in left["tables"] + right["tables"]}) == 4
    for binding in (one, two):
        with rig.allocator().attach(rig.context, binding, read_only=True) as connection:
            test_same_table_in_two_original_schemas(connection)
        rig.removed(binding)


def test_partial_allocation_reconciles_owned_only(rig) -> None:
    """Interrupt actual namespace/prepared checkpoints; resume recorded ownership only."""
    from dango.storage.catalog_ownership import OwnedWorkspace

    original = OwnedWorkspace.checkpoint
    for phase in ("namespace", "prepared"):

        def interrupt(owned, native, checkpoint="prepared", expected=phase):
            original(owned, native, checkpoint)
            if checkpoint == expected:
                raise InterruptedError("fixed-checkpoint")

        with patch.object(OwnedWorkspace, "checkpoint", interrupt):
            rig.denied(rig.allocator().allocate, rig.context, phase)
        saved = rig.current(phase)
        assert saved.state == "recovery_required"
        recovered = rig.allocator().reconcile(rig.context, saved)
        assert recovered.binding_id == saved.binding_id and recovered.state == "ready"
        assert rig.allocator().reconcile(rig.context, recovered) == recovered
        rig.removed(recovered)


def test_foreign_and_concurrent_additions_refused(rig) -> None:
    """Independent native DDL after intent must abort deletion and preserve foreign objects."""
    binding = rig.populate("concurrent")
    before = rig.metadata(binding)
    original = rig.port.remove_owned
    foreign = '"' + binding.metadata_schema + '".foreign_object'

    def race(value, credential, evidence, intent, **kwargs):
        def add(files):
            intent(files)
            rig.sql("CREATE TABLE " + foreign + "(canary INTEGER)", execute=True)

        return original(value, credential, evidence, add, **kwargs)

    with patch.object(rig.port, "remove_owned", race):
        rig.denied(rig.allocator().remove_owned, rig.context, binding)
    assert rig.sql("SELECT count(*) FROM " + foreign) == [(0,)]
    assert rig.metadata(binding)["namespace"] == before["namespace"]
    assert rig.metadata(binding)["tables"] == before["tables"]
    rig.denied(rig.allocator().reconcile, rig.context, rig.current("concurrent"))
    rig.sql("DROP TABLE " + foreign + " RESTRICT", execute=True)
    rig.removed(binding)
    unknown = rig.populate("unknown")
    rig.sql('COMMENT ON SCHEMA "' + unknown.metadata_schema + "\" IS 'foreign'", execute=True)
    rig.denied(rig.allocator().reconcile, rig.context, unknown)
    rig.denied(rig.allocator().remove_owned, rig.context, unknown)
    assert rig.sql(
        f"SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname='{unknown.metadata_schema}'"
    ) == [("foreign",)]
    rig.sql(
        f"COMMENT ON SCHEMA \"{unknown.metadata_schema}\" IS 'dango:{unknown.binding_id}'",
        execute=True,
    )
    rig.removed(unknown)


def test_populated_removal_recovery_and_physical_addition(rig) -> None:
    """Crash after actual native drop and during unlink; preserve later physical additions."""
    from dango.storage.catalog_ownership import OwnedWorkspace

    for phase in ("native", "physical"):
        binding = rig.populate("remove-" + phase)
        original = os.unlink

        def unlink(name, *args, saved=original, **kwargs):
            saved(name, *args, **kwargs)
            if str(name).endswith(".parquet"):
                raise InterruptedError("fixed-unlink")

        target = (
            patch.object(
                OwnedWorkspace, "native_removed", side_effect=InterruptedError("fixed-drop")
            )
            if phase == "native"
            else patch("os.unlink", unlink)
        )
        with target:
            rig.denied(rig.allocator().remove_owned, rig.context, binding)
        name = binding.metadata_schema
        assert not rig.sql(f"SELECT oid FROM pg_namespace WHERE nspname='{name}'")
        addition = Path(binding.data_root_ref) / "foreign.sentinel"
        addition.write_text("preserve")
        rig.denied(rig.allocator().remove_owned, rig.context, rig.current(binding.workspace_id))
        assert addition.read_text() == "preserve"
        addition.unlink()
        rig.removed(binding)


def test_current_authority_and_secret_denials(rig) -> None:
    """Current identity/grant/persisted ref checks precede any native connection."""
    binding = rig.populate("authority")
    for change in (
        {"actor_id": "other"},
        {"project_id": "other"},
        {"permission_revision": "stale"},
    ):
        context = rig.context.model_copy(update=change)
        for action in ("attach", "reconcile", "remove_owned"):

            def operation(action=action, context=context):
                if action == "attach":
                    with rig.allocator().attach(context, binding, read_only=True):
                        pass
                else:
                    getattr(rig.allocator(), action)(context, binding)

            rig.control["resolves"] = 0
            with patch.object(rig.port, "_connect", wraps=rig.port._connect) as calls:
                rig.denied(operation)
                assert not calls.called and rig.control["resolves"] == 0
    for current in (None, rig.ref.model_copy(update={"version": "v2"})):
        rig.control["current"] = current
        with patch.object(rig.port, "_connect", wraps=rig.port._connect) as calls:
            rig.denied(rig.allocator().reconcile, rig.context, binding)
            assert not calls.called
    rig.control.update(current=rig.ref, grant=False, resolves=0)
    with patch.object(rig.port, "_connect", wraps=rig.port._connect) as calls:
        rig.denied(rig.allocator().remove_owned, rig.context, binding)
        assert not calls.called and rig.control["resolves"] == 0
    rig.control["grant"] = True
    rig.removed(binding)


def test_dsn_and_secret_canary_redacted(rig) -> None:
    """Real native authentication failure must cross only the sanitized public boundary."""
    binding = rig.populate("canary")
    password = rig.credential["password"]
    canary = "T04A-secret-canary-'\\-private"
    rig.credential["password"] = canary
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        rig.denied(rig.allocator().reconcile, rig.context, binding)
    rig.credential["password"] = password
    payload = captured.getvalue() + "".join(
        p.read_text() for p in (rig.root / "state").glob("*.json")
    )
    assert canary not in payload and password not in payload and "host=" not in payload
    rig.removed(binding)


CASES = [
    test_native_prepare_and_reopen_no_schema_mutation,
    test_distinct_catalog_and_data_roots,
    test_partial_allocation_reconciles_owned_only,
    test_foreign_and_concurrent_additions_refused,
    test_populated_removal_recovery_and_physical_addition,
    test_current_authority_and_secret_denials,
    test_dsn_and_secret_canary_redacted,
]


def main() -> None:
    """Run integration smoke first, then only its gated finite matrix; retain all statuses."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--acquire", type=Path)
    parser.add_argument("--task-root", type=Path)
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.acquire:
        try:
            acquire(args.acquire)
        except urllib.error.HTTPError as error:
            stage = args.acquire / "work/acquisition-stage.json"
            stage.write_text(
                json.dumps({**json.loads(stage.read_text()), "http_status": error.code})
            )
            raise SystemExit("Frozen artifact HTTP status " + str(error.code)) from None
        return
    assert args.task_root and args.commit
    report = {
        "status": "FAIL",
        "commit": args.commit,
        "classification": "integration",
        "cases": {case.__name__: "UNRUN" for case in CASES},
    }
    output = args.task_root.parent / "native-result.json"
    try:
        rig = NativeRig(args.task_root)
        for index, case in enumerate(CASES):
            report["cases"][case.__name__] = "FAIL"
            (args.task_root.parent / "native-stage.txt").write_text(case.__name__)
            case(rig)
            report["observations"] = rig.observations
            report["pins"] = {
                "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
                "python": sys.version,
                "duckdb": sys.modules["duckdb"].__version__,
            }
            report["cases"][case.__name__] = "PASS"
            if not index:
                report.update(status="PASS_NATIVE_ADAPTER_SMOKE", owned_cleanup=True)
                (args.task_root.parent / "smoke-result.json").write_text(json.dumps(report) + "\n")
            output.write_text(json.dumps(report) + "\n")
        assert all(status == "PASS" for status in report["cases"].values())
        report["status"] = "PASS_NATIVE_ADAPTER_MATRIX"
    except Exception as error:
        report.update(status="FAIL", error_type=type(error).__name__)
        raise SystemExit("Native integration failed; inspect sanitized case report") from None
    finally:
        output.write_text(json.dumps(report) + "\n")


if __name__ == "__main__":
    main()
