"""tests/integration/input_observations.py

Keep independent native fixture, raw journal/file and strict refusal oracles for the finite12 cases.
"""

from __future__ import annotations

import hashlib
import json
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


AFFECTED = tuple(f"{n:02d}" for n in range(5, 13))
RETAINED = {
    "head": "fa2282c0bba6f959ca9a2d12d314cecc395c46df",
    "run": "45861634718",
    "retained_case_ids": ["01", "02", "03", "04"],
    "cases": {
        f"{n:02d}": ("PASS" if n <= 4 else ("FAIL" if n == 5 else "UNRUN")) for n in range(1, 13)
    },
}
