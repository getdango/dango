"""dango/transformation/artifacts.py

Parse the admitted native dbt JSON profile and preserve opaque diagnostics without executing code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from dango.transformation.artifact_records import (
    SCHEMAS,
    ArtifactError,
    ArtifactEvidence,
    ArtifactSet,
    BuildPlan,
    BuildReceipt,
    Command,
    InvocationBinding,
    NodeOutcome,
    SupportFeature,
    logical_path,
)

__all__ = [
    "ArtifactError",
    "ArtifactEvidence",
    "ArtifactSet",
    "BuildPlan",
    "BuildReceipt",
    "InvocationBinding",
    "NodeOutcome",
    "SupportFeature",
    "parse_artifacts",
]
INVOCATION_FILE = "invocation.json"
MACRO_DIGEST = "3ce57b4d7e6c253042e03517ad1a8179ea9108958265fc43dee2a7e79fa80b60"
COVERAGE = "coverage:manifest-and-declared-path-hashes-only;source-bodies-not-inspected"


def _json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        """Reject duplicate members before ordinary JSON object construction."""
        value = dict(items)
        if len(value) != len(items):
            raise ArtifactError()
        return value

    def invalid(value: str) -> Any:
        """Refuse nonfinite JSON constants without retaining their text."""
        raise ArtifactError()

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)

    def finite(item: Any) -> None:
        """Reject overflowed numbers even inside opaque native fields."""
        if isinstance(item, dict):
            for child in item.values():
                finite(child)
        elif isinstance(item, list):
            for child in item:
                finite(child)
        elif type(item) is float and not (-float("inf") < item < float("inf")):
            raise ArtifactError()

    finite(value)
    return value


def _read(root: int, name: str) -> bytes:
    logical_path(name)
    parent = os.dup(root)
    try:
        parts = name.split("/")
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ArtifactError()
            raw = stream.read()
            after = os.fstat(stream.fileno())
            fields = (
                "st_dev",
                "st_ino",
                "st_uid",
                "st_gid",
                "st_nlink",
                "st_mode",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if any(getattr(after, field) != getattr(before, field) for field in fields):
                raise ArtifactError()
            return raw
    finally:
        os.close(parent)


def _text(value: Any) -> str:
    if type(value) is not str or not value.strip():
        raise ArtifactError()
    return value


def _map(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ArtifactError()
    return value


def _metadata(value: dict[str, Any], name: str, invocation: str) -> None:
    metadata = _map(value["metadata"])
    if (
        metadata["dbt_schema_version"] != SCHEMAS[name]
        or metadata["dbt_version"] != "2.0.8"
        or _text(metadata["invocation_id"]) != invocation
    ):
        raise ArtifactError("artifact_binding_mismatch")
    timestamp = re.fullmatch(
        r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
        r"(?:\.([0-9]{1,9}))?(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
        _text(metadata["generated_at"]),
    )
    if timestamp is None:
        raise ArtifactError()
    clock, fraction, offset = timestamp.groups()
    # Validate native nanosecond timestamps on Python3.10 without altering evidence bytes.
    fraction = "." + fraction.ljust(6, "0")[:6] if fraction else ""
    offset = "+00:00" if offset == "Z" else offset
    if datetime.fromisoformat(clock + fraction + offset).utcoffset() is None:
        raise ArtifactError()


def _inventory(manifest: dict[str, Any], binding: InvocationBinding) -> tuple[SupportFeature, ...]:
    result: list[SupportFeature] = []
    root = _text(_map(manifest["metadata"])["project_name"])
    macros = _map(manifest["macros"])
    content = {key: _map(value) for key, value in macros.items()}
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()

    def add(feature: Any, identity: str, supported: bool, pointer: str) -> None:
        """Attach immutable private evidence and explicit source-coverage limits."""
        result.append(
            SupportFeature(
                feature=feature,
                identity=identity,
                disposition="observed_admitted" if supported else "unsupported",
                evidence_refs=(binding.artifacts["manifest.json"].evidence_ref + pointer, COVERAGE),
            )
        )

    if digest != MACRO_DIGEST:
        add("macro", "macro-content-set:" + digest, False, "#/macros")
    packages: set[str] = set()
    for key, value in macros.items():
        macro = _map(value)
        if macro["unique_id"] != key:
            raise ArtifactError()
        package = _text(macro["package_name"])
        packages.add(package)
        add(
            "macro",
            _text(key),
            digest == MACRO_DIGEST and package in {"dbt", "dbt_duckdb"},
            "#/macros/" + key,
        )
    for key, value in _map(manifest["nodes"]).items():
        node = _map(value)
        if node["unique_id"] != key:
            raise ArtifactError()
        kind = _text(node["resource_type"])
        package = _text(node["package_name"])
        packages.add(package)
        config = _map(node["config"])
        language = node.get("language")
        if kind in {"model", "snapshot", "test"}:
            language = _text(language)
            if language != "sql":
                add("python", key + ":" + language, False, "#/nodes/" + key)
        materialization = _text(config["materialized"])
        admitted = {
            "model": {"table", "incremental"},
            "seed": {"seed"},
            "test": {"test"},
            "snapshot": {"snapshot"},
        }
        supported = materialization in admitted.get(kind, set())
        if materialization == "incremental":
            supported = supported and config.get("incremental_strategy") == "delete+insert"
        add("materialization", key + ":" + materialization, supported, "#/nodes/" + key + "/config")
        for hook_name in ("pre-hook", "post-hook"):
            hooks = config.get(hook_name, [])
            if type(hooks) is not list:
                raise ArtifactError()
            for index, hook in enumerate(hooks):
                if type(hook) not in {str, dict}:
                    raise ArtifactError()
                add(
                    "hook",
                    key + ":" + hook_name + ":" + str(index),
                    False,
                    "#/nodes/" + key + "/config/" + hook_name,
                )
    for key, value in _map(manifest["sources"]).items():
        source = _map(value)
        if source["unique_id"] != key or source["resource_type"] != "source":
            raise ArtifactError()
        packages.add(_text(source["package_name"]))
    for key, values in _map(manifest["disabled"]).items():
        if type(values) is not list:
            raise ArtifactError()
        add("materialization", "disabled:" + key, False, "#/disabled/" + key)
    for package in sorted(packages - {root}):
        add(
            "package",
            package,
            package in {"dbt", "dbt_duckdb"} and digest == MACRO_DIGEST,
            "#/macros",
        )
    for path in binding.supplied_project_files:
        if path.endswith(".py"):
            add("python", path, False, "#declared-project-files")
        elif path.startswith("macros/"):
            add("macro", path, False, "#declared-project-files")
        elif path in {"packages.yml", "dependencies.yml", "package-lock.yml"}:
            add("package", path, False, "#declared-project-files")
    return tuple(result)


def _outcomes(
    manifest: dict[str, Any], results: dict[str, Any], binding: InvocationBinding
) -> tuple[NodeOutcome, ...]:
    command = binding.plan.command
    expected = "compile" if command == "docs_generate" else command
    args = _map(results["args"])
    rows = results["results"]
    if args["command"] != expected or args["which"] != expected:
        raise ArtifactError("artifact_binding_mismatch")
    if type(rows) is not list or (command == "docs_generate" and rows):
        raise ArtifactError()
    nodes = _map(manifest["nodes"])
    outcomes = []
    statuses = {
        "success": "success",
        "pass": "success",
        "fail": "failed",
        "error": "error",
        "skipped": "skipped",
        "warn": "warn",
    }
    for index, item in enumerate(rows):
        row = _map(item)
        uid = _text(row["unique_id"])
        kind = _text(_map(nodes[uid])["resource_type"])
        if command == "snapshot" and kind != "snapshot":
            raise ArtifactError("artifact_binding_mismatch")
        native_status = _text(row["status"])
        if native_status not in statuses or (native_status in {"pass", "fail"} and kind != "test"):
            raise ArtifactError("artifact_unsupported")
        failures = row["failures"]
        if failures is not None and (type(failures) is not int or failures < 0):
            raise ArtifactError()
        if native_status in {"success", "pass"} and failures not in {None, 0}:
            raise ArtifactError()
        outcomes.append(
            NodeOutcome.model_validate(
                {
                    "node_id": uid,
                    "kind": kind
                    if kind in {"model", "seed", "snapshot", "test", "source"}
                    else "other",
                    "status": statuses[native_status],
                    "evidence_refs": (
                        binding.artifacts["run_results.json"].evidence_ref
                        + "#/results/"
                        + str(index),
                    ),
                }
            )
        )
    return tuple(outcomes)


def parse_artifacts(
    target_dir: Path, expected_commit: str, expected_input_set_id: str, expected_command: Command
) -> ArtifactSet:
    """Verify contained exact bytes and the runner binding before returning native outcomes.

    The owning runner attests provenance; this is not authentication of a hostile runner.
    Logs are opaque diagnostics. No subprocess, network, source execution or writes occur.
    Contained reads require directory-relative no-follow IO; unsupported hosts refuse safely.
    """
    descriptor = -1
    try:
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            raise ArtifactError("artifact_unsupported")
        descriptor = os.open(target_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        raw = _read(descriptor, INVOCATION_FILE)
        _json(raw)
        binding = InvocationBinding.model_validate_json(raw)
        if (binding.plan.code_commit, binding.plan.input_set_id, binding.plan.command) != (
            expected_commit,
            expected_input_set_id,
            expected_command,
        ):
            raise ArtifactError("artifact_binding_mismatch")
        try:
            os.stat("catalog.json", dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if "catalog.json" not in binding.artifacts:
                raise ArtifactError("artifact_binding_mismatch")
        parsed = {}
        for name, evidence in {**binding.artifacts, **binding.diagnostics}.items():
            raw = _read(descriptor, name)
            if (
                len(raw) != evidence.byte_length
                or hashlib.sha256(raw).hexdigest() != evidence.sha256
            ):
                raise ArtifactError("artifact_binding_mismatch")
            if name in binding.artifacts:
                value = _map(_json(raw))
                _metadata(value, name, binding.invocation_id)
                parsed[name] = value
        manifest, results = parsed["manifest.json"], parsed["run_results.json"]
        return ArtifactSet(
            command=binding.plan.command,
            invocation_id=binding.invocation_id,
            code_commit=binding.plan.code_commit,
            input_set_id=binding.plan.input_set_id,
            schema_versions={name: SCHEMAS[name] for name in binding.artifacts},
            artifact_hashes={
                name: item.sha256
                for name, item in {**binding.artifacts, **binding.diagnostics}.items()
            },
            artifact_refs={
                name: item.evidence_ref
                for name, item in {**binding.artifacts, **binding.diagnostics}.items()
            },
            node_outcomes=_outcomes(manifest, results, binding),
            binding=binding,
            support_inventory=_inventory(manifest, binding),
        )
    except ArtifactError:
        raise
    except FileNotFoundError:
        raise ArtifactError("artifact_missing") from None
    except (OSError, ValueError, TypeError, KeyError, RecursionError, ValidationError):
        raise ArtifactError() from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
