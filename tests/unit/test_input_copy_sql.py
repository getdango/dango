"""tests/unit/test_input_copy_sql.py

Prove structured copy statements preserve hostile-looking identifiers, values and constraints.
"""

from contextlib import nullcontext
from pathlib import Path

import duckdb

from dango.storage.input_copy import (
    NativeInputCopy,
    _create_sql,
    _describe_sql,
    _export_sql,
    _insert_sql,
)
from dango.storage.input_state import Column, InputCaptureError, Relation, fingerprint


def exercise_sql_roundtrip(connection, root, negative=None):
    """Use independent literal native names/rows/schema answers; also reused by native case04."""
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection.execute('CREATE SCHEMA catalog."src""; DROP TABLE sentinel; --"')
    connection.execute('CREATE SCHEMA catalog."dst space"')
    connection.execute('CREATE TABLE catalog."dst space".sentinel(value INTEGER)')
    connection.execute('INSERT INTO catalog."dst space".sentinel VALUES (99)')
    columns = (
        Column(name='id"; DROP TABLE sentinel; --', type="INTEGER", nullable=False),
        Column(name="amount space", type="BIGINT", nullable=True),
    )
    source = Relation(
        relation_id="sql-source",
        schema_name='src"; DROP TABLE sentinel; --',
        table_name="orders.dot",
        table_uuid="source-id",
        columns=columns,
        schema_fingerprint=fingerprint(columns),
        source_version_ref="sql-source-v1",
        checkpoint_ref="sql-checkpoint-v1",
    )
    target = source.model_copy(
        update={"schema_name": "dst space", "table_name": "orders'; SELECT 9; --"}
    )
    connection.execute(_create_sql(source))
    connection.execute(
        'INSERT INTO catalog."src""; DROP TABLE sentinel; --"."orders.dot" VALUES (1,7),(2,NULL)'
    )
    path = Path(root) / "literal'; DROP TABLE sentinel; --.parquet"
    connection.execute(_export_sql(source), [str(path)])
    assert path.is_file()
    connection.execute(_create_sql(target))
    connection.execute(_insert_sql(target), [str(path)])
    expected_columns = [
        ('id"; DROP TABLE sentinel; --', "INTEGER", "NO"),
        ("amount space", "BIGINT", "YES"),
    ]
    assert [
        row[:3] for row in connection.execute(_describe_sql(target)).fetchall()
    ] == expected_columns
    observed = connection.execute(
        "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
        "WHERE table_catalog='catalog' AND table_schema=? AND table_name=? ORDER BY ordinal_position",
        ["dst space", "orders'; SELECT 9; --"],
    ).fetchall()
    assert observed == expected_columns
    select = 'SELECT * FROM catalog."dst space"."orders\'; SELECT 9; --" ORDER BY 1'
    assert connection.execute(select).fetchall() == [(1, 7), (2, None)]
    with negative() if negative is not None else nullcontext():
        try:
            connection.execute(
                'INSERT INTO catalog."dst space"."orders\'; SELECT 9; --" VALUES(NULL,7)'
            )
        except duckdb.ConstraintException:
            pass
        else:
            raise AssertionError("NOT NULL constraint was lost")
    try:
        connection.execute(_create_sql(target))
    except duckdb.CatalogException:
        pass
    else:
        raise AssertionError("Existing target was overwritten")
    assert connection.execute(select).fetchall() == [(1, 7), (2, None)]
    assert connection.execute('SELECT * FROM catalog."dst space".sentinel').fetchall() == [(99,)]
    try:
        NativeInputCopy._collisions(connection, (target,))
    except InputCaptureError as error:
        assert error.code == "capture_collision"
    else:
        raise AssertionError("Preflight collision was admitted")
    invalid = source.model_copy(
        update={
            "columns": (
                columns[0].model_copy(update={"type": "INTEGER); DROP TABLE sentinel; --"}),
            )
        }
    )
    try:
        _create_sql(invalid)
    except InputCaptureError as error:
        assert error.code == "capture_schema_changed"
    else:
        raise AssertionError("Unadmitted type became SQL")
    return {
        "adversarial_names_preserved": True,
        "file_value_bound": True,
        "ordered_schema": expected_columns,
        "rows": [(1, 7), (2, None)],
        "null_and_collision_refused": True,
        "sentinel": 99,
    }


def test_copy_sql_preserves_identifiers_values_schema_and_collisions(tmp_path):
    with duckdb.connect() as connection:
        connection.execute("ATTACH ':memory:' AS catalog")
        exercise_sql_roundtrip(connection, tmp_path)


FIXTURE_REFUSALS = (
    "historical_reference",
    "changed_original",
    "changed_root",
    "symlink",
    "hardlink",
    "late_tamper",
    "replaced_inode",
    "multiple_new",
    "preexisting_unknown",
    "wrong_stage",
    "ordinary_workspace",
    "unsafe_reference",
)


def pytest_generate_tests(metafunc):
    """Keep pytest out of native04's import closure while enumerating local refusal factors."""
    if "factor" in metafunc.fixturenames:
        metafunc.parametrize("factor", FIXTURE_REFUSALS)


def _rejected_write_rig(root, *, preexisting=False, wrong_stage=False):
    from datetime import timedelta

    from dango.storage.catalogs import CatalogError
    from tests.integration.input_sql_fixture import RejectedWrite, checkpoint_report
    from tests.unit.workspace_support import Rig

    root.mkdir(mode=0o700)
    rig = Rig(root)
    owner, workspace = rig.allocate()
    binding = rig.allocator.allocate(owner, workspace.workspace_id)
    data = Path(binding.data_root_ref)
    (data / "known.parquet").write_bytes(b"known-original")
    if preexisting:
        (data / "unknown.parquet").write_bytes(b"preserve-unknown")
    references = {"data": [["", True, "", True, "known.parquet", True, None]], "delete": []}
    guard = RejectedWrite(
        binding,
        lambda: references,
        {},
        lambda: checkpoint_report({}, rig, root / "fixture-receipt.json"),
    )
    rig.sql_guard = guard

    def remove(binding, credential, evidence, intent, *, resuming):
        if wrong_stage:
            raise CatalogError()
        path = rig.port.root / binding.binding_id
        if path.exists():
            rig.port.inspect_owned(binding, credential, evidence)
            intent({"known.parquet"})
            path.unlink()
        elif not resuming:
            raise AssertionError("Unrecorded synthetic native removal")

    rig.port.remove_owned = remove
    rig.now += timedelta(days=8)
    return rig, workspace, guard, references, data


def test_rejected_write_cleanup_requires_actual_failure_and_same_expiry_operation(tmp_path):
    from tests.integration.input_sql_fixture import expire_fixture

    rig, workspace, guard, _, data = _rejected_write_rig(tmp_path / "positive")
    candidate = data / "negative.parquet"
    with guard.negative():
        candidate.write_bytes(b"own-negative")
    assert candidate.exists() and guard.evidence["removed"] == []
    original_remove = guard._remove

    def observed_unlink(name, identity):
        import json

        saved = json.loads((rig.root / "fixture-receipt.json").read_text())["fixture_lifecycle"]
        assert saved["pre_unlink_receipt"]["removed"] == []
        assert (
            saved["actual_expiry_failure"]["classification"]
            == "OwnedWorkspace.removal_intent:OwnershipError"
        )
        assert saved["before_conditional_cleanup"]["files"][name] == identity
        return original_remove(name, identity)

    guard._remove = observed_unlink
    receipt = expire_fixture(rig, workspace)
    assert receipt.status == "success" and not data.exists()
    proof = guard.evidence
    assert (
        proof["actual_expiry_failure"]["classification"]
        == "OwnedWorkspace.removal_intent:OwnershipError"
    )
    assert proof["failed_operation"] == {
        "state": "prepared",
        "receipt": None,
        "allocator_phase": "prepared",
    }
    assert (
        proof["same_operation_retry"]["operation_id"]
        == proof["actual_expiry_failure"]["operation_id"]
    )
    assert proof["actual_expiry_failure"]["expected_revision"] == 0
    assert proof["removed"] == [
        {
            "path": "negative.parquet",
            "identity": proof["after_negative"]["files"]["negative.parquet"],
        }
    ]
    assert rig.rows(
        "SELECT count(*) FROM platform_operations WHERE operation_name='workspace.expire'"
    ) == [(1,)]
    assert rig.rows(
        "SELECT state,status FROM platform_operations WHERE operation_name='workspace.expire'"
    ) == [("finalized", "success")]


def test_rejected_write_without_residual_needs_no_fixture_cleanup(tmp_path):
    from tests.integration.input_sql_fixture import expire_fixture

    rig, workspace, guard, _, data = _rejected_write_rig(tmp_path / "no-residual")
    with guard.negative():
        pass
    assert expire_fixture(rig, workspace).status == "success"
    assert not data.exists() and guard.evidence["removed"] == [] and not guard.used
    assert "actual_expiry_failure" not in guard.evidence


def test_rejected_write_fixture_refuses_ambiguous_removal(tmp_path, factor):
    import os

    import pytest

    from dango.storage.catalog_ownership import OwnershipError
    from dango.storage.workspaces import WorkspaceError
    from tests.integration.input_sql_fixture import FixtureRefusal, expire_fixture

    rig, workspace, guard, references, data = _rejected_write_rig(
        tmp_path / factor,
        preexisting=factor == "preexisting_unknown",
        wrong_stage=factor == "wrong_stage",
    )
    candidate = data / "negative.parquet"
    with guard.negative():
        candidate.write_bytes(b"new")
        if factor == "multiple_new":
            (data / "second.parquet").write_bytes(b"also-new")
    if factor == "historical_reference":
        references["delete"].append(["", True, "", True, "negative.parquet", True, 3])
    elif factor == "unsafe_reference":
        references["data"].append(["", True, "..", True, "outside.parquet", True, None])
    elif factor == "changed_original":
        (data / "known.parquet").write_bytes(b"changed")
    elif factor == "changed_root":
        data.chmod(0o755)
    elif factor == "symlink":
        candidate.unlink()
        candidate.symlink_to(data / "known.parquet")
    elif factor == "hardlink":
        candidate.unlink()
        os.link(data / "known.parquet", candidate)
    elif factor == "replaced_inode":
        replacement = data / "replacement"
        replacement.write_bytes(b"new")
        replacement.replace(candidate)
    elif factor == "late_tamper":
        original = guard.read_references
        calls = []

        def tamper():
            calls.append(1)
            if len(calls) == 2:
                candidate.write_bytes(b"bad")
            return original()

        guard.read_references = tamper
    elif factor == "ordinary_workspace":
        rig.sql_guard = None
    with pytest.raises((FixtureRefusal, OwnershipError, WorkspaceError)):
        expire_fixture(rig, workspace)
    assert candidate.exists() and guard.evidence["removed"] == []
    assert (data / "known.parquet").exists()
    assert (rig.port.root / guard.binding.binding_id).exists()
    if factor not in {"wrong_stage", "ordinary_workspace"}:
        assert guard.evidence["actual_expiry_failure"]["workspace_id"] == workspace.workspace_id
