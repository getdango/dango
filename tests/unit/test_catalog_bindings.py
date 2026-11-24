"""tests/unit/test_catalog_bindings.py

Independent local ownership/failure oracles; native engine acceptance is a separate entrypoint.
"""

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from dango.operations.contracts import OperationContext
from dango.security.native_credentials import SecretRef
from dango.storage.catalog_ownership import (
    OwnershipError,
    catalog_file_paths,
    restrict_drop_sql,
)
from dango.storage.catalogs import (
    CatalogAllocator,
    CatalogBinding,
    CatalogError,
    NativeCatalogPort,
    _credential,
    _dsn,
    _literal,
)

pytestmark = pytest.mark.unit
CANARY = "private-canary-'\\-password"


class NativeDouble(NativeCatalogPort):
    """Supply failure injection only; this double establishes no native compatibility."""

    def __init__(self):
        self.catalogs = {}
        self.calls = []
        self.fail = None

    def prepare(self, binding, credential, checkpoint, evidence=None):
        self.calls.append("prepare")
        identity = {
            "namespace": [[41, "writer", "dango:" + binding.binding_id]],
            "relations": [[42, "ducklake_metadata", "r"]],
        }
        self.catalogs[binding.binding_id] = identity
        checkpoint(identity, "prepared")
        if self.fail == "prepare":
            raise RuntimeError(CANARY)

    def inspect_owned(self, binding, credential, evidence):
        self.calls.append("inspect")
        if self.catalogs.get(binding.binding_id) != evidence:
            raise RuntimeError(CANARY)
        return evidence

    @contextmanager
    def attach(self, binding, credential, *, read_only):
        self.calls.append(("attach", read_only))
        yield object()

    def remove_owned(self, binding, credential, evidence, intent, *, resuming):
        self.calls.append("remove")
        if binding.binding_id not in self.catalogs and resuming:
            return
        self.inspect_owned(binding, credential, evidence)
        intent(
            {
                str(p.relative_to(binding.data_root_ref))
                for p in Path(binding.data_root_ref).rglob("*.parquet")
            }
        )
        del self.catalogs[binding.binding_id]
        if self.fail == "remove":
            raise RuntimeError(CANARY)


@pytest.fixture
def rig(tmp_path):
    state, data = tmp_path / "state", tmp_path / "data"
    state.mkdir(mode=0o700)
    data.mkdir(mode=0o700)
    context = OperationContext(
        operation_id="op",
        project_id="project",
        actor_id="owner",
        capability="catalog.admin",
        permission_revision="r1",
        trace_id="trace",
    )
    ref = SecretRef(project_id="project", actor_id="owner", credential_id="secret", version="v1")
    control = {"grant": True, "current": ref, "resolved": [], "authorized": []}
    credential = {
        "host": "127.0.0.1",
        "port": "5432",
        "dbname": "task",
        "user": "writer",
        "password": CANARY,
    }
    port = NativeDouble()

    def authorize(ctx, binding, action):
        control["authorized"].append(action)
        return control["grant"] and ctx.permission_revision == "r1"

    def resolve(reference):
        control["resolved"].append(reference)
        return credential if reference == control["current"] else None

    allocator = CatalogAllocator(
        state,
        data,
        port=port,
        credential_ref=ref,
        catalog_database="task",
        bundle_id="development",
        resolve=resolve,
        authorize=authorize,
    )
    return allocator, context, port, control, state, data


def record(state):
    return json.loads(next(state.glob("*.json")).read_text())


def current(rig):
    return CatalogBinding.model_validate(record(rig[4])["binding"])


def test_distinct_catalog_and_data_roots(rig):
    allocator, context, _, _, _, _ = rig
    one = allocator.allocate(context, "one")
    two = allocator.allocate(context, "two")
    assert one.metadata_schema != two.metadata_schema
    assert one.binding_id != two.binding_id
    assert one.data_root_ref != two.data_root_ref
    assert Path(one.data_root_ref).stat().st_ino != Path(two.data_root_ref).stat().st_ino


@pytest.mark.parametrize("operation", ["attach", "reconcile", "remove"])
@pytest.mark.parametrize(
    "change", [{"actor_id": "other"}, {"project_id": "other"}, {"permission_revision": "revoked"}]
)
def test_current_identity_denied_before_secret_and_native(rig, operation, change):
    allocator, context, port, control, _, _ = rig
    binding = allocator.allocate(context, "one")
    control["resolved"].clear()
    port.calls.clear()
    wrong = OperationContext.model_validate({**context.model_dump(), **change})
    with pytest.raises(CatalogError):
        if operation == "attach":
            with allocator.attach(wrong, binding, read_only=True):
                pass
        elif operation == "reconcile":
            allocator.reconcile(wrong, binding)
        else:
            allocator.remove_owned(wrong, binding)
    assert not control["resolved"] and not port.calls


@pytest.mark.parametrize("revocation", ["grant", "secret"])
def test_current_revocation_refuses_native(rig, revocation):
    allocator, context, port, control, _, _ = rig
    binding = allocator.allocate(context, "one")
    if revocation == "grant":
        control["grant"] = False
    else:
        control["current"] = None
    port.calls.clear()
    control["resolved"].clear()
    with pytest.raises(CatalogError):
        with allocator.attach(context, binding, read_only=True):
            pass
    assert not port.calls
    assert len(control["resolved"]) == (revocation == "secret")


def test_persisted_revision_and_credential_ref_cannot_be_forged(rig):
    allocator, context, port, control, _, _ = rig
    binding = allocator.allocate(context, "one")
    port.calls.clear()
    control["resolved"].clear()
    for forged in (
        binding.model_copy(update={"revision": 0}),
        binding.model_copy(
            update={"credential_ref": binding.credential_ref.model_copy(update={"version": "old"})}
        ),
    ):
        with pytest.raises(CatalogError):
            allocator.reconcile(context, forged)
    assert not port.calls and not control["resolved"]


def test_partial_allocation_reconciles_owned_only(rig):
    allocator, context, port, _, state, _ = rig
    port.fail = "prepare"
    with pytest.raises(CatalogError):
        allocator.allocate(context, "one")
    saved = current(rig)
    assert saved.state == "recovery_required"
    assert record(state)["phase"] == "prepared"
    recovered = allocator.reconcile(context, saved)
    assert recovered.state == "ready" and recovered.revision > saved.revision
    port.catalogs[recovered.binding_id] = {"foreign": True}
    with pytest.raises(CatalogError):
        allocator.remove_owned(context, recovered)
    assert Path(recovered.data_root_ref).is_dir()


@pytest.mark.parametrize("interrupt", ["after_native", "during_files"])
def test_populated_removal_recovers_without_deleting_unknown_objects(rig, monkeypatch, interrupt):
    allocator, context, port, _, state, _ = rig
    binding = allocator.allocate(context, "one")
    root = Path(binding.data_root_ref)
    (root / "sales").mkdir()
    for name in ("a.parquet", "b.parquet"):
        (root / "sales" / name).write_bytes(b"owned")
    if interrupt == "after_native":
        port.fail = "remove"
    else:
        original = os.unlink
        count = 0

        def fail_once(path, *args, **kwargs):
            nonlocal count
            if str(path).endswith(".parquet"):
                count += 1
                if count == 2:
                    raise OSError(CANARY)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", fail_once)
    with pytest.raises(CatalogError):
        allocator.remove_owned(context, binding)
    assert record(state)["phase"] in {"removing", "native_removed"}
    saved = current(rig)
    (root / "unknown.txt").write_text("foreign")
    with pytest.raises(CatalogError):
        allocator.remove_owned(context, saved)
    assert (root / "unknown.txt").read_text() == "foreign"
    (root / "unknown.txt").unlink()
    allocator.remove_owned(context, saved)
    assert not root.exists() and record(state)["phase"] == "removed"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "replaced_root"])
def test_physical_substitution_refused_before_native(rig, tmp_path, kind):
    allocator, context, port, _, _, _ = rig
    binding = allocator.allocate(context, "one")
    root = Path(binding.data_root_ref)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    if kind == "symlink":
        (root / "escape").symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, root / "escape")
    else:
        root.rename(root.with_name("retained-original"))
        root.mkdir(mode=0o700)
    port.calls.clear()
    with pytest.raises(CatalogError):
        with allocator.attach(context, binding, read_only=True):
            pass
    assert not port.calls and outside.read_text() == "untouched"


def test_dsn_and_secret_canary_redacted(rig, caplog):
    allocator, context, port, _, state, _ = rig
    port.fail = "prepare"
    with pytest.raises(CatalogError) as error:
        allocator.allocate(context, "one")
    assert CANARY not in str(error.value) + caplog.text + next(state.glob("*.json")).read_text()
    assert error.value.__suppress_context__
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": True},
        {"revision": -1},
        {"extra": 1},
        {"project_id": " "},
        {"data_root_ref": "../bad"},
        {"metadata_schema": "public"},
    ],
)
def test_strict_binding_records(rig, changes):
    binding = rig[0].allocate(rig[1], "one")
    with pytest.raises(ValueError):
        CatalogBinding.model_validate({**binding.model_dump(), **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"port": "0"},
        {"port": "65536"},
        {"port": "+5432"},
        {"port": 5432},
        {"host": ""},
        {"dsn": "authority"},
        {"dbname": "other"},
    ],
)
def test_connection_mapping_is_narrow_and_strict(changes):
    value = {
        "host": "local",
        "port": "5432",
        "dbname": "task",
        "user": "writer",
        "password": CANARY,
    }
    with pytest.raises(CatalogError):
        _credential({**value, **changes}, "task")


def test_escaping_has_distinct_libpq_and_sql_boundaries():
    assert _dsn({"password": "a'\\b"}) == "password='a\\'\\\\b'"
    assert _literal("a'b") == "'a''b'"


def test_native_paths_reject_escape_and_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "owned.parquet").write_bytes(b"x")
    rows = [("", True, "", True, "owned.parquet", True, None)]
    assert catalog_file_paths(rows, root) == {"owned.parquet"}
    for bad in ("../outside", "/outside"):
        with pytest.raises(OwnershipError):
            catalog_file_paths([("", True, "", True, bad, True, None)], root)
    (root / "owned.parquet").unlink()
    (root / "owned.parquet").symlink_to(tmp_path / "outside")
    with pytest.raises(OSError):
        catalog_file_paths(rows, root)


def test_destructive_statement_uses_atomic_restrict_and_identity_guards():
    evidence = {
        "namespace": [[41, "writer", "marker"]],
        "dependents": "expected",
        "relations": [[42, "ducklake_metadata", "r"]],
    }
    sql = restrict_drop_sql(
        "dango_" + "a" * 32,
        "a" * 32,
        "/owned/root",
        "SELECT snapshot",
        "0,1",
        evidence,
        "SELECT dependencies",
    )
    assert "CASCADE" not in sql
    assert sql.startswith("DO $dango$ BEGIN LOCK TABLE")
    assert "ACCESS EXCLUSIVE" in sql and sql.count(" RESTRICT;") == 2
    assert "nspowner=current_user::regrole" in sql and "obj_description" in sql
    assert "oid NOT IN (42)" in sql and "IS DISTINCT FROM 41" in sql
    assert "data_path" in sql and "version" in sql


def test_read_only_intent_cannot_be_coerced(rig):
    allocator, context, port, _, _, _ = rig
    binding = allocator.allocate(context, "one")
    port.calls.clear()
    with pytest.raises(CatalogError):
        with allocator.attach(context, binding, read_only=1):
            pass
    assert not port.calls


def test_import_and_native_entrypoint_collection_have_no_runtime_io():
    import subprocess
    import sys

    source = Path(__file__).resolve().parents[2]
    code = """
import builtins, os, pathlib, runpy, socket, sqlite3, subprocess, sys, urllib.request
from unittest.mock import patch
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
original_open = builtins.open

def read_only(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in "wax+"):
        raise AssertionError("import write")
    return original_open(file, mode, *args, **kwargs)

with patch("builtins.open", read_only), patch("os.mkdir", side_effect=AssertionError("mkdir")), patch("socket.socket", side_effect=AssertionError("network")), patch("sqlite3.connect", side_effect=AssertionError("database")), patch("subprocess.Popen", side_effect=AssertionError("process")), patch("urllib.request.urlopen", side_effect=AssertionError("egress")):
    import dango.storage.catalogs as catalogs
    assert pathlib.Path(catalogs.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1]))
    runpy.run_path(sys.argv[1] + "/tests/integration/native_catalog_bindings.py")
    assert "duckdb" not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code, str(source)], check=True, capture_output=True)
