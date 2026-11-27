"""tests/unit/test_dbt2_records.py

Validate strict direct constructors, identity joins and detached immutable record serialization.
"""

import json
import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from dango.transformation.artifacts import (
    ArtifactError,
    ArtifactEvidence,
    ArtifactSet,
    BuildPlan,
    BuildReceipt,
    InvocationBinding,
)
from tests.unit.test_dbt2_artifacts import (
    HASHES,
    digest,
    fixture,
    parse,
    rebind,
    receipt,
    write_json,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "field",
    [
        "plan_hash",
        "workspace_id",
        "input_set_id",
        "code_commit",
        "bundle_id",
        "invocation_id",
        "exit_code",
    ],
)
def test_direct_receipt_identity_cannot_drift(tmp_path, field):
    target, _ = fixture(tmp_path)
    parsed = parse(target)
    value = 1 if field == "exit_code" else ("0" * 64 if field == "plan_hash" else "different")
    with pytest.raises(ValidationError):
        receipt(parsed, **{field: value})
    healthy = receipt(parsed)
    assert BuildReceipt.model_validate_json(healthy.model_dump_json()) == healthy


def test_nested_maps_and_instances_are_immutable(tmp_path):
    target, binding = fixture(tmp_path)
    parsed = parse(target)
    binding["artifacts"]["manifest.json"]["sha256"] = "0" * 64
    dumped = parsed.model_dump(mode="json")
    dumped["artifact_hashes"]["manifest.json"] = "0" * 64
    assert parsed.artifact_hashes["manifest.json"] == HASHES["build/manifest.json"]
    for mapping in (
        parsed.artifact_hashes,
        parsed.artifact_refs,
        parsed.schema_versions,
        parsed.binding.artifacts,
        parsed.binding.diagnostics,
        parsed.binding.supplied_project_files,
    ):
        with pytest.raises(TypeError):
            mapping["new"] = "value"
    with pytest.raises(ValidationError):
        parsed.binding.exit_code = 1
    assert ArtifactSet.model_validate_json(parsed.model_dump_json()) == parsed
    unsafe = parsed.model_copy(update={"code_commit": "wrong"})
    with pytest.raises(ValidationError):
        receipt(unsafe)


@pytest.mark.parametrize(
    "factor", ["schema", "hash", "ref", "required_missing", "duplicate", "command"]
)
def test_direct_artifact_set_cannot_bypass_binding(tmp_path, factor):
    target, _ = fixture(tmp_path)
    parsed = parse(target)
    value = parsed.model_dump(mode="json")
    if factor == "schema":
        value["schema_versions"]["manifest.json"] = "unknown"
    if factor == "hash":
        value["artifact_hashes"]["manifest.json"] = "0" * 64
    if factor == "ref":
        value["artifact_refs"]["manifest.json"] = "unrelated"
    if factor == "required_missing":
        value["node_outcomes"].pop()
    if factor == "duplicate":
        value["node_outcomes"].append(value["node_outcomes"][0])
    if factor == "command":
        value["command"] = "snapshot"
    with pytest.raises(ValidationError):
        ArtifactSet.model_validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "factor",
    [
        "bool_version",
        "bool_exit",
        "float_exit",
        "extra",
        "required_empty",
        "required_duplicate",
        "bad_plan_hash",
        "bad_path",
        "self_reference",
        "bool_length",
        "blank_ref",
    ],
)
def test_strict_binding_records(tmp_path, factor):
    target, binding = fixture(tmp_path)
    if factor == "bool_version":
        binding["schema_version"] = True
    if factor == "bool_exit":
        binding["exit_code"] = False
    if factor == "float_exit":
        binding["exit_code"] = 0.0
    if factor == "extra":
        binding["extra"] = "not admitted"
    if factor == "required_empty":
        binding["required_node_ids"] = []
    if factor == "required_duplicate":
        binding["required_node_ids"] *= 2
    if factor == "bad_plan_hash":
        binding["plan"]["selectors"] = ["changed"]
    if factor == "bad_path":
        binding["supplied_project_files"] = {"../outside.sql": "0" * 64}
    if factor == "self_reference":
        binding["diagnostics"]["invocation.json"] = binding["artifacts"]["manifest.json"]
    if factor == "bool_length":
        binding["artifacts"]["manifest.json"]["byte_length"] = True
    if factor == "blank_ref":
        binding["artifacts"]["manifest.json"]["evidence_ref"] = " "
    with pytest.raises(ValidationError):
        InvocationBinding.model_validate_json(json.dumps(binding))
    with pytest.raises(ValidationError):
        ArtifactEvidence(sha256="0" * 64, byte_length=False, evidence_ref="ref")
    with pytest.raises(ValidationError):
        BuildPlan.model_validate_json(json.dumps(binding["plan"] | {"unknown": 1}))


def test_non_success_lifecycle_requires_safe_reason_and_never_infers_it():
    value = {
        "plan_hash": "0" * 64,
        "workspace_id": "w",
        "input_set_id": "i",
        "code_commit": "c",
        "bundle_id": "b",
        "invocation_id": "v",
        "exit_code": -15,
        "artifacts": None,
    }
    for status, code, action in [
        ("failed", "build_failed", "inspect_private_build_evidence"),
        ("cancelled", "build_cancelled", "retry_build"),
        ("recovery_required", "build_recovery_required", "recover_build"),
    ]:
        assert BuildReceipt(**value, status=status, error_code=code, action=action).status == status
        with pytest.raises(ValidationError):
            BuildReceipt(**value, status=status)
        with pytest.raises(ValidationError):
            BuildReceipt(**value, status=status, error_code=code, action="raw native secret")
    with pytest.raises(ValidationError):
        BuildReceipt(**value, status="success")


@pytest.mark.parametrize("folder", [False, True])
def test_symlink_artifacts_and_diagnostics_refuse(tmp_path, folder):
    target, binding = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "native.log").write_text("synthetic diagnostic")
    evidence = {
        "sha256": digest(b"synthetic diagnostic"),
        "byte_length": 20,
        "evidence_ref": "private:opaque",
    }
    if folder:
        (target / "logs").symlink_to(outside, target_is_directory=True)
        binding["diagnostics"] = {"logs/native.log": evidence}
    else:
        (outside / "manifest.json").write_bytes((target / "manifest.json").read_bytes())
        (target / "manifest.json").unlink()
        (target / "manifest.json").symlink_to(outside / "manifest.json")
    write_json(target / "invocation.json", binding)
    with pytest.raises(ArtifactError):
        parse(target)


def test_file_content_change_during_read_refuses(tmp_path):
    target, _ = fixture(tmp_path)
    original = os.fdopen

    class ChangingRead:
        def __init__(self, descriptor, *args):
            self.stream = original(descriptor, *args)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self):
            raw = self.stream.read()
            # Actual same-file write at the read boundary, not a fabricated stat result.
            with (target / "invocation.json").open("ab") as output:
                output.write(b" ")
            return raw

    with patch("dango.transformation.artifacts.os.fdopen", ChangingRead):
        with pytest.raises(ArtifactError):
            parse(target)


@pytest.mark.parametrize(
    ("timestamp", "accepted"),
    [
        ("2026-11-10T13:35:23Z", True),
        ("2026-11-10T13:35:23.1Z", True),
        ("2026-11-10T13:35:23.12Z", True),
        ("2026-11-10T13:35:23.123Z", True),
        ("2026-11-10T13:35:23.1234Z", True),
        ("2026-11-10T13:35:23.12345Z", True),
        ("2026-11-10T13:35:23.123456Z", True),
        ("2026-11-10T13:35:23.1234567Z", True),
        ("2026-11-10T13:35:23.12345678Z", True),
        ("2026-11-10T13:35:23.123456789Z", True),
        ("2024-02-29T23:59:59.123456789+05:30", True),
        ("2026-11-10T13:35:23.123456789-03:30", True),
        ("2026-11-10T13:35:23.123456789", False),
        ("2026-11-10T13:35:23.1234567890Z", False),
        ("2026-11-10T13:35:23.abcZ", False),
        ("2026-11-10T13:35:23.Z", False),
        ("2026-02-29T13:35:23Z", False),
        ("2026-13-10T13:35:23Z", False),
        ("2026-11-10T24:35:23Z", False),
        ("2026-11-10T13:60:23Z", False),
        ("2026-11-10T13:35:60Z", False),
        ("2026-11-10T13:35:23+24:00", False),
        ("2026-11-10T13:35:23+00:60", False),
        ("2026-11-10T13:35:23+0530", False),
        ("2026-11-10 13:35:23Z", False),
        ("2026-11-10T13:35:23Ztrailing", False),
        (None, False),
        (True, False),
    ],
)
def test_native_timestamp_precision_calendar_and_offset(tmp_path, timestamp, accepted):
    target, binding = fixture(tmp_path)
    value = json.loads((target / "run_results.json").read_text())
    value["metadata"]["generated_at"] = timestamp  # Explicit synthetic timestamp variant.
    rebind(target, binding, "run_results.json", value)
    if accepted:
        assert receipt(parse(target)).status == "success"
    else:
        with pytest.raises(ArtifactError):
            parse(target)
