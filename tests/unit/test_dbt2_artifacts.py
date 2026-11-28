"""tests/unit/test_dbt2_artifacts.py

Check original native byte fixtures and explicitly synthetic runner bindings and corrupt variants.
"""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from dango.transformation.artifacts import (
    ArtifactError,
    BuildReceipt,
    parse_artifacts,
)

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parents[1] / "fixtures/dbt2_artifacts"
HASHES = {
    "build/manifest.json": "277ded597218e39b8279bd27a43e1088590a1006149d432cf9e30c3bdd929029",
    "build/run_results.json": "a8182892e78a885996e49159338af782737f3d9c193dd770f6475678165d933a",
    "build/catalog.json": "dcc62f87c52e5d5a731e7db68059caad29a9cba2d7e4f376c582ca013965bae7",
    "snapshot/manifest.json": "6a98b433919ad3a109aaf3cc6e0a462e595fecc537414e63d30efd4db20f258e",
    "snapshot/run_results.json": "67bac66ae10e36d1d7788603ef856b4b9248cbcec14be927e097550f60794c35",
    "docs_generate/manifest.json": "f596924f683ca244debe6ef6eea41ef2e222086f51121eb3c5f05ad85ccf3b2c",
    "docs_generate/run_results.json": "485ee1f52a6224282f321517e1d22ad00a5d796a4a1592392945dd672c9df1e1",
}
REQUIRED = {
    "build": [
        "seed.t44a_probe.input",
        "model.t44a_probe.orders",
        "test.t44a_probe.engine_and_total",
    ],
    "snapshot": ["snapshot.t44a_probe.asset_history"],
    "docs_generate": [],
}


ARCHIVE_HASH = "97b5247dd15f13bdb3122317cf1c0e7d68e3840e7dd799df0eba0a1ac77180dd"
LENGTHS = {
    "build/manifest.json": 503382,
    "build/run_results.json": 2273,
    "build/catalog.json": 1358,
    "snapshot/manifest.json": 503242,
    "snapshot/run_results.json": 1027,
    "docs_generate/manifest.json": 503436,
    "docs_generate/run_results.json": 346,
}


def archive_members():
    """Read only fixed, bounded original streams from the derived lossless test package."""
    raw = (FIXTURES / "native-artifacts.zip").read_bytes()
    assert len(raw) == 209708 and hashlib.sha256(raw).hexdigest() == ARCHIVE_HASH
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        assert len(entries) == 7 and set(archive.namelist()) == set(HASHES) == set(LENGTHS)
        assert sum(info.file_size for info in entries) == 1515064
        for info in entries:
            assert not info.is_dir() and stat.S_ISREG(info.external_attr >> 16)
            assert info.file_size == LENGTHS[info.filename]
            assert not info.flag_bits & 1 and info.compress_type == zipfile.ZIP_DEFLATED
        members = {name: archive.read(name) for name in HASHES}
    for name, data in members.items():
        assert len(data) == LENGTHS[name] and hashlib.sha256(data).hexdigest() == HASHES[name]
    return members


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value))


def fixture(tmp_path, command="build", exit_code=0):
    """Construct a synthetic runner envelope around original native bytes."""
    target = tmp_path / "target"
    assert command in REQUIRED
    target.mkdir()
    for name, raw in archive_members().items():
        if name.startswith(command + "/"):
            (target / Path(name).name).write_bytes(raw)
    plan = {
        "command": command,
        "plan_id": "plan",
        "workspace_id": "workspace",
        "code_commit": "code-commit",
        "input_set_id": "input-set",
        "definition_revision": "definition",
        **{
            name: []
            for name in (
                "selectors",
                "start_nodes",
                "included_descendants",
                "deferred_upstreams",
                "expected_outputs",
                "destructive_changes",
                "required_capabilities",
                "known_impacts",
                "possible_impacts",
                "unknown_impacts",
            )
        },
    }
    plan["plan_hash"] = digest(
        json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    metadata = json.loads((target / "manifest.json").read_text())["metadata"]
    binding = {
        "schema_version": 1,
        "plan": plan,
        "invocation_id": metadata["invocation_id"],
        "bundle_id": "bundle",
        "exit_code": exit_code,
        "warn_policy": "error",
        "required_node_ids": list(REQUIRED[command]),
        "native_profile": "t44a-dbt-oss-2.0.8-manifest12-results6",
        "artifacts": {},
        "supplied_project_files": {},
        "diagnostics": {},
    }
    for path in target.iterdir():
        raw = path.read_bytes()
        binding["artifacts"][path.name] = {
            "sha256": digest(raw),
            "byte_length": len(raw),
            "evidence_ref": "private:sha256:" + digest(raw),
        }
    write_json(target / "invocation.json", binding)
    return target, binding


def rebind(target, binding, name, value):
    """Labelled synthetic mutation updates only its declared byte evidence."""
    raw = json.dumps(value).encode() if not isinstance(value, bytes) else value
    (target / name).write_bytes(raw)
    binding["artifacts"][name] = {
        "sha256": digest(raw),
        "byte_length": len(raw),
        "evidence_ref": "synthetic:sha256:" + digest(raw),
    }
    write_json(target / "invocation.json", binding)


def parse(target, command="build"):
    return parse_artifacts(target, "code-commit", "input-set", command)


def receipt(artifacts, **changes):
    binding = artifacts.binding
    value = {
        "plan_hash": binding.plan.plan_hash,
        "workspace_id": "workspace",
        "input_set_id": "input-set",
        "code_commit": "code-commit",
        "bundle_id": "bundle",
        "invocation_id": binding.invocation_id,
        "exit_code": binding.exit_code,
        "status": "success",
        "artifacts": artifacts,
    }
    return BuildReceipt(**(value | changes))


@pytest.mark.parametrize("command", REQUIRED)
def test_authentic_command_pairs(tmp_path, command):
    target, _ = fixture(tmp_path, command)
    with (
        patch("socket.socket", side_effect=AssertionError("network")),
        patch("subprocess.run", side_effect=AssertionError("process")),
    ):
        parsed = parse(target, command)
    assert [n.node_id for n in parsed.node_outcomes] == REQUIRED[command]
    assert all(n.status == "success" for n in parsed.node_outcomes)
    assert all(f.disposition == "observed_admitted" for f in parsed.support_inventory)
    assert {f.identity for f in parsed.support_inventory if f.feature == "package"} == {
        "dbt",
        "dbt_duckdb",
    }
    assert receipt(parsed).status == "success"
    if command == "docs_generate":
        native = json.loads((target / "run_results.json").read_text())
        assert native["args"]["command"] == "compile" and native["results"] == []


@pytest.mark.parametrize("status", ["fail", "error", "warn", "skipped"])
def test_failed_test_is_failed_receipt(tmp_path, status):
    target, binding = fixture(tmp_path)
    value = json.loads((target / "run_results.json").read_text())
    value["results"][2]["status"] = status
    value["results"][2]["failures"] = 0
    rebind(target, binding, "run_results.json", value)
    diagnostic = b"SYNTHETIC: Completed with success; all tests passed"
    (target / "diagnostic.log").write_bytes(diagnostic)
    binding["diagnostics"] = {
        "diagnostic.log": {
            "sha256": digest(diagnostic),
            "byte_length": len(diagnostic),
            "evidence_ref": "synthetic:misleading-success",
        }
    }
    write_json(target / "invocation.json", binding)
    parsed = parse(target)
    assert parsed.node_outcomes[2].status == ("failed" if status == "fail" else status)
    with pytest.raises(ValidationError):
        receipt(parsed)
    failed = receipt(
        parsed, status="failed", error_code="build_failed", action="inspect_private_build_evidence"
    )
    assert failed.status == "failed"
    assert failed.artifacts.artifact_hashes["diagnostic.log"] == digest(diagnostic)


@pytest.mark.parametrize("exit_code", [1, -15])
def test_nonzero_exit_cannot_become_success(tmp_path, exit_code):
    target, _ = fixture(tmp_path, exit_code=exit_code)
    parsed = parse(target)
    with pytest.raises(ValidationError):
        receipt(parsed)
    with pytest.raises(ValidationError):
        receipt(parsed, exit_code=0)
    assert (
        receipt(
            parsed,
            status="failed",
            error_code="build_failed",
            action="inspect_private_build_evidence",
        ).exit_code
        == exit_code
    )


@pytest.mark.parametrize(
    "factor",
    [
        "missing",
        "null",
        "truncated",
        "duplicates",
        "nonfinite",
        "overflow",
        "version",
        "status",
        "timezone",
        "boolean_failures",
        "missing_outcome",
        "duplicate_outcome",
        "missing_macros",
        "wrong_result_kind",
        "unbound_catalog",
        "diagnostic_missing",
        "diagnostic_changed",
    ],
)
def test_missing_null_or_truncated_artifact_is_actionable(tmp_path, factor):
    target, binding = fixture(tmp_path)
    name = "run_results.json"
    value = json.loads((target / name).read_text())
    if factor == "missing":
        (target / name).unlink()
    elif factor in {"null", "truncated", "duplicates", "nonfinite", "overflow"}:
        raw = {
            "null": b"null",
            "truncated": b'{"metadata":',
            "duplicates": b'{"x":1,"x":2}',
            "nonfinite": b'{"x":NaN}',
            "overflow": b'{"x":1e999}',
        }[factor]
        rebind(target, binding, name, raw)
    elif factor == "unbound_catalog":
        binding["artifacts"].pop("catalog.json")
        write_json(target / "invocation.json", binding)
    elif factor.startswith("diagnostic_"):
        binding["diagnostics"] = {
            "logs/native.log": {
                "sha256": "0" * 64,
                "byte_length": 0,
                "evidence_ref": "private:diagnostic",
            }
        }
        if factor == "diagnostic_changed":
            (target / "logs").mkdir()
            (target / "logs/native.log").write_text("Completed with success")
        write_json(target / "invocation.json", binding)
    else:
        if factor == "version":
            value["metadata"]["dbt_schema_version"] = "unknown"
        if factor == "status":
            value["results"][0]["status"] = "Completed with success"
        if factor == "timezone":
            value["metadata"]["generated_at"] = "2026-11-10T13:35:23"
        if factor == "boolean_failures":
            value["results"][2]["failures"] = False
        if factor == "missing_outcome":
            value["results"].pop()
        if factor == "duplicate_outcome":
            value["results"].append(value["results"][0])
        if factor == "missing_macros":
            name = "manifest.json"
            value = json.loads((target / name).read_text())
            value.pop("macros")
        if factor == "wrong_result_kind":
            value["results"][0]["status"] = "pass"
        rebind(target, binding, name, value)
    with pytest.raises(ArtifactError) as error:
        parse(target)
    assert error.value.code in {
        "artifact_missing",
        "artifact_invalid",
        "artifact_unsupported",
        "artifact_binding_mismatch",
    }
    assert error.value.action and str(error.value) == error.value.code


@pytest.mark.parametrize(
    "factor",
    ["invocation", "input", "commit", "command", "native_command", "hash", "length", "pair"],
)
def test_stale_invocation_or_input_mismatch_rejected(tmp_path, factor):
    target, binding = fixture(tmp_path)
    if factor == "invocation":
        binding["invocation_id"] = "stale"
    if factor == "hash":
        binding["artifacts"]["manifest.json"]["sha256"] = "0" * 64
    if factor == "length":
        binding["artifacts"]["manifest.json"]["byte_length"] += 1
    write_json(target / "invocation.json", binding)
    if factor in {"native_command", "pair"}:
        value = json.loads((target / "run_results.json").read_text())
        if factor == "native_command":
            value["args"]["command"] = "snapshot"
        else:
            value["metadata"]["invocation_id"] = "different-native-invocation"
        rebind(target, binding, "run_results.json", value)
    args = ["code-commit", "input-set", "build"]
    if factor in {"commit", "input", "command"}:
        args[{"commit": 0, "input": 1, "command": 2}[factor]] = "wrong"
    with pytest.raises(ArtifactError):
        parse_artifacts(target, *args)


def test_unsupported_python_exception_is_visible(tmp_path):
    runtime = json.loads((FIXTURES.parent / "runtime_profiles.json").read_text())
    original = runtime["receipts"]["42825753494:assets-python-run_results.json"]
    raw = original["payload"].encode()
    assert digest(raw) == original["source_sha256"]
    native = json.loads(raw)
    assert native["args"]["command"] == "run" and native["results"][0]["status"] == "error"
    assert "Python models are not supported for duckdb adapter" in native["results"][0]["message"]
    target, binding = fixture(tmp_path)
    manifest = json.loads((target / "manifest.json").read_text())
    manifest["nodes"]["model.t44a_probe.orders"]["language"] = "python"
    rebind(
        target, binding, "manifest.json", manifest
    )  # Synthetic build variant, not native Python build.
    parsed = parse(target)
    assert any(
        f.feature == "python" and f.disposition == "unsupported" for f in parsed.support_inventory
    )
    with pytest.raises(ValidationError):
        receipt(parsed)
    assert (
        receipt(
            parsed,
            status="failed",
            error_code="artifact_unsupported",
            action="review_support_exception",
        ).status
        == "failed"
    )


def test_artifacts_preserved_by_hash(tmp_path):
    provenance = json.loads((FIXTURES / "provenance.json").read_text())
    assert len(provenance["files"]) == 7
    members = archive_members()
    assert provenance["package"]["sha256"] == ARCHIVE_HASH
    assert provenance["package"]["byte_length"] == 209708
    assert (
        provenance["package"]["classification"]
        == "derived_lossless_test_container_not_native_dbt_archive"
    )
    for entry in provenance["files"]:
        raw = members[entry["archive_member"]]
        assert digest(raw) == HASHES[entry["archive_member"]] == entry["sha256"]
        assert len(raw) == entry["byte_length"]
    target, _ = fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    parsed = parse(target)
    assert before == {p.name: p.read_bytes() for p in target.iterdir()}
    assert parsed.artifact_hashes["manifest.json"] == HASHES["build/manifest.json"]


@pytest.mark.parametrize(
    "factor",
    [
        "macro",
        "macro_metadata",
        "package",
        "hook",
        "materialization",
        "project_python",
        "empty_macros",
    ],
)
def test_supplied_unsupported_features_refuse_success(tmp_path, factor):
    target, binding = fixture(tmp_path)
    manifest = json.loads((target / "manifest.json").read_text())
    node = manifest["nodes"]["model.t44a_probe.orders"]
    if factor == "macro":
        next(iter(manifest["macros"].values()))["macro_sql"] += "\n-- synthetic changed content"
    if factor == "macro_metadata":
        next(iter(manifest["macros"].values()))["arguments"] = [
            {"name": "synthetic_changed_argument"}
        ]
    if factor == "empty_macros":
        manifest["macros"] = {}
    if factor == "package":
        node["package_name"] = "custom_package"
    if factor == "hook":
        node["config"]["pre-hook"] = ["synthetic hook body is never executed"]
    if factor == "materialization":
        node["config"]["materialized"] = "custom_materialization"
    if factor == "project_python":
        binding["supplied_project_files"] = {"models/custom.py": "1" * 64}
    rebind(target, binding, "manifest.json", manifest)
    parsed = parse(target)
    assert any(f.disposition == "unsupported" for f in parsed.support_inventory)
    with pytest.raises(ValidationError):
        receipt(parsed)
