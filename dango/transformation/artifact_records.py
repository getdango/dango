"""dango/transformation/artifact_records.py

Define immutable build provenance and conservative outcome records without performing IO.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


def nonblank(value: str) -> str:
    """Reject whitespace-only identifiers without normalizing their identity."""
    if not value.strip():
        raise ValueError("artifact_invalid")
    return value


def logical_path(value: str) -> str:
    """Admit only portable contained logical paths, never evidence URLs."""
    if any(c in value for c in ("\\", ":", "\x00")) or any(
        part in {"", ".", ".."} for part in value.split("/")
    ):
        raise ValueError("artifact_invalid")
    return value


Text = Annotated[str, Field(min_length=1), AfterValidator(nonblank)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
LogicalPath = Annotated[Text, AfterValidator(logical_path)]
Command = Literal["build", "snapshot", "docs_generate"]
SCHEMAS = {
    "manifest.json": "https://schemas.getdbt.com/dbt/manifest/v12.json",
    "run_results.json": "https://schemas.getdbt.com/dbt/run-results/v6.json",
    "catalog.json": "https://schemas.getdbt.com/dbt/catalog/v1.json",
}
SAFE_ACTIONS = {
    "artifact_invalid": "regenerate_bound_artifacts",
    "artifact_missing": "regenerate_bound_artifacts",
    "artifact_binding_mismatch": "rerun_bound_invocation",
    "artifact_unsupported": "review_support_exception",
    "build_failed": "inspect_private_build_evidence",
    "build_cancelled": "retry_build",
    "build_recovery_required": "recover_build",
}


class ArtifactError(ValueError):
    """Expose only fixed safe codes and actions, never native artifact content."""

    def __init__(self, code: str = "artifact_invalid") -> None:
        self.code = code
        self.action = SAFE_ACTIONS[code]
        super().__init__(code)


class Record(BaseModel):
    """Validate public records strictly, including nested existing instances."""

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False, revalidate_instances="always"
    )


class BuildPlan(Record):
    """Bind one ordered selection and impact assessment to its canonical digest."""

    command: Command
    plan_id: Text
    workspace_id: Text
    code_commit: Text
    input_set_id: Text
    definition_revision: Text
    plan_hash: Digest
    selectors: tuple[Text, ...]
    start_nodes: tuple[Text, ...]
    included_descendants: tuple[Text, ...]
    deferred_upstreams: tuple[Text, ...]
    expected_outputs: tuple[Text, ...]
    destructive_changes: tuple[Text, ...]
    required_capabilities: tuple[Text, ...]
    known_impacts: tuple[Text, ...]
    possible_impacts: tuple[Text, ...]
    unknown_impacts: tuple[Text, ...]

    @model_validator(mode="after")
    def verify_hash(self) -> BuildPlan:
        """Check all declared plan fields, retaining ordered selection tuples."""
        raw = json.dumps(
            self.model_dump(mode="json", exclude={"plan_hash"}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        if hashlib.sha256(raw).hexdigest() != self.plan_hash:
            raise ValueError("artifact_binding_mismatch")
        return self


class ArtifactEvidence(Record):
    """Identify immutable private bytes without treating their reference as a path."""

    sha256: Digest
    byte_length: Annotated[int, Field(ge=0)]
    evidence_ref: Text


class SupportFeature(Record):
    """Describe only supplied content and explicitly expose unsupported features."""

    feature: Literal["python", "package", "macro", "hook", "materialization"]
    identity: Text
    disposition: Literal["observed_admitted", "unsupported"]
    evidence_refs: tuple[Text, ...]


class InvocationBinding(Record):
    """Retain a runner assertion of plan, outcomes and exact byte provenance."""

    schema_version: Literal[1]
    plan: BuildPlan
    invocation_id: Text
    bundle_id: Text
    exit_code: int
    warn_policy: Literal["error"]
    required_node_ids: tuple[Text, ...]
    native_profile: Literal["t44a-dbt-oss-2.0.8-manifest12-results6"]
    artifacts: Mapping[LogicalPath, ArtifactEvidence]
    supplied_project_files: Mapping[LogicalPath, Digest]
    diagnostics: Mapping[LogicalPath, ArtifactEvidence]

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: Any) -> Any:
        """Keep booleans distinct from the numeric schema version."""
        if type(value) is not int:
            raise ValueError("artifact_invalid")
        return value

    @field_validator("artifacts", "supplied_project_files", "diagnostics")
    @classmethod
    def freeze_maps(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        """Snapshot validated entries and deny mutable aliases."""
        return MappingProxyType(dict(value))

    @field_serializer("artifacts", "supplied_project_files", "diagnostics")
    def serialize_maps(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Return detached ordinary JSON objects, never mapping proxies."""
        return {
            key: item.model_dump(mode="json") if isinstance(item, Record) else item
            for key, item in value.items()
        }

    @model_validator(mode="after")
    def complete_selection(self) -> InvocationBinding:
        """Require a finite selected set and the admitted per-command artifact names."""
        names = set(self.artifacts)
        required = self.required_node_ids
        if (
            not {"manifest.json", "run_results.json"} <= names <= SCHEMAS.keys()
            or names & self.diagnostics.keys()
            or "invocation.json" in self.diagnostics
            or len(set(required)) != len(required)
            or bool(required) != (self.plan.command != "docs_generate")
        ):
            raise ValueError("artifact_invalid")
        return self


class NodeOutcome(Record):
    """Separate selected native node results from command execution status."""

    node_id: Text
    kind: Literal["model", "seed", "snapshot", "test", "source", "other"]
    status: Literal["success", "failed", "error", "skipped", "warn"]
    evidence_refs: tuple[Text, ...]


class ArtifactSet(Record):
    """Retain public immutable binding and inventory alongside parsed outcomes."""

    command: Command
    invocation_id: Text
    code_commit: Text
    input_set_id: Text
    schema_versions: Mapping[Text, Text]
    artifact_hashes: Mapping[Text, Digest]
    artifact_refs: Mapping[Text, Text]
    node_outcomes: tuple[NodeOutcome, ...]
    binding: InvocationBinding
    support_inventory: tuple[SupportFeature, ...]

    @field_validator("schema_versions", "artifact_hashes", "artifact_refs")
    @classmethod
    def freeze_maps(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        """Copy immutable scalar mappings before exposing them."""
        return MappingProxyType(dict(value))

    @field_serializer("schema_versions", "artifact_hashes", "artifact_refs")
    def serialize_maps(self, value: Mapping[str, str]) -> dict[str, str]:
        """Produce detached JSON maps for callers and round trips."""
        return dict(value)

    @model_validator(mode="after")
    def identities_and_outcomes(self) -> ArtifactSet:
        """Reject independent identity maps and missing or duplicate selected results."""
        b = self.binding
        entries = {**b.artifacts, **b.diagnostics}
        ids = [node.node_id for node in self.node_outcomes]
        if (
            (self.command, self.invocation_id, self.code_commit, self.input_set_id)
            != (b.plan.command, b.invocation_id, b.plan.code_commit, b.plan.input_set_id)
            or dict(self.schema_versions) != {name: SCHEMAS[name] for name in b.artifacts}
            or dict(self.artifact_hashes) != {name: item.sha256 for name, item in entries.items()}
            or dict(self.artifact_refs)
            != {name: item.evidence_ref for name, item in entries.items()}
            or len(set(ids)) != len(ids)
            or not set(b.required_node_ids) <= set(ids)
            or (self.command == "docs_generate" and ids)
        ):
            raise ValueError("artifact_binding_mismatch")
        return self


class BuildReceipt(Record):
    """Validate outcomes against the complete retained invocation, including direct callers."""

    plan_hash: Digest
    workspace_id: Text
    input_set_id: Text
    code_commit: Text
    bundle_id: Text
    invocation_id: Text
    exit_code: int
    status: Literal["success", "failed", "cancelled", "recovery_required"]
    artifacts: ArtifactSet | None
    error_code: str | None = None
    action: str | None = None
    evidence_refs: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def strict_outcome(self) -> BuildReceipt:
        """Forbid success from bad checks, unsupported scope or independent identity claims.

        P0 warns and all skipped outcomes conservatively prevent success, including
        skipped non-test nodes. Cancellation/recovery require an explicit safe reason.
        """
        if self.artifacts is not None:
            b = self.artifacts.binding
            p = b.plan
            if (self.plan_hash, self.workspace_id, self.input_set_id, self.code_commit) != (
                p.plan_hash,
                p.workspace_id,
                p.input_set_id,
                p.code_commit,
            ) or (self.bundle_id, self.invocation_id, self.exit_code) != (
                b.bundle_id,
                b.invocation_id,
                b.exit_code,
            ):
                raise ValueError("artifact_binding_mismatch")
        if self.status == "success":
            if (
                self.exit_code != 0
                or self.artifacts is None
                or self.error_code is not None
                or self.action is not None
                or any(n.status != "success" for n in self.artifacts.node_outcomes)
                or any(f.disposition == "unsupported" for f in self.artifacts.support_inventory)
            ):
                raise ValueError("build_failed")
        elif self.error_code not in SAFE_ACTIONS or self.action != SAFE_ACTIONS[self.error_code]:
            raise ValueError("artifact_invalid")
        return self
