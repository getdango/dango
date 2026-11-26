"""dango/storage/input_state.py

Validate the fixed accepted fixture, live lifetime lock and private capture journal.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from dango.operations.contracts import OperationContext
from dango.storage.catalogs import CatalogBinding
from dango.storage.workspace_records import Counter, Record, Text, Token, WorkspacePin
from dango.storage.workspace_state import LockedRecord, _owned, _unique_pairs, current_binding

if TYPE_CHECKING:
    from dango.storage.catalogs import CatalogAllocator
    from dango.storage.inputs import InputSet
    from dango.storage.workspaces import WorkspaceService


class InputCaptureError(RuntimeError):
    """Return fixed safe errors, never private paths, SQL or native diagnostics."""

    def __init__(self, code: str):
        self.code = code
        self.action = {
            "capture_invalid": "Supply unique admitted relation IDs and a nonnegative byte budget.",
            "capture_denied": "Use the owning server-admitted capture operation and current grants.",
            "capture_quota_exceeded": "Review selected input bytes and admit a new capture budget.",
            "capture_source_unavailable": "Restore the exact accepted fixture and live lifetime authority.",
            "capture_schema_changed": "Review the accepted snapshot schema and immutable input manifest.",
            "capture_collision": "Use an owned target without the selected schema or table names.",
            "capture_recovery_required": "Inspect retained owned effects before explicit recovery or cleanup.",
            "capture_released": "Admit a new capture before using released historical input metadata.",
            "capture_unavailable": "Inspect the private capture journal before retrying.",
        }[code]
        super().__init__(f"{code}: {self.action}")


class Column(Record):
    """Describe ordered admitted columns independently of current production state."""

    name: Text
    type: Text
    nullable: bool


def fingerprint(columns: tuple[Column, ...]) -> str:
    """Hash explicit ordered names, native types and nullability using stable JSON."""
    value = [column.model_dump() for column in columns]
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Relation(Record):
    """Resolve one opaque relation ID through server-installed accepted metadata."""

    relation_id: Text
    schema_name: Text
    table_name: Text
    table_uuid: Text
    columns: tuple[Column, ...]
    schema_fingerprint: Text
    source_version_ref: Text
    checkpoint_ref: Text


class AcceptedFixture(Record):
    """Admit exactly the controlled P0 fixture, never a general source registry."""

    kind: Literal["accepted-input-fixture-v1"]
    fixture_id: Literal["p0-selected-inputs-v1"]
    accepted_release_id: Literal["p0-accepted-v1"]
    project_id: Text
    actor_id: Text
    catalog_id: Text
    binding: CatalogBinding
    snapshot_id: Counter
    relations: tuple[Relation, ...]
    retention_mode: Literal["exclusive_fixture_lifetime"]
    maintenance_mode: Literal["disabled"]
    lifetime_nonce: Token


class Guardian(Record):
    """Bind the live controller-held lock to this exact private source session."""

    kind: Literal["input-fixture-lifetime-v1"]
    nonce: Token
    project_id: Text
    actor_id: Text
    source_workspace_id: Text
    source_binding_id: Token
    manifest_sha256: Text


class CapturePin(Record):
    """Keep exact authoritative T04B pin ownership for release and inspection."""

    workspace_id: Token
    pin: WorkspacePin


class StageFile(Record):
    """Record the exact owned staging inode, never infer deletion authority from its name."""

    index: Counter
    device: Counter
    inode: Counter
    size: Counter


class CaptureRecord(Record):
    """Persist domain intent/results without copying operation lifecycle authority."""

    input_set_id: Token
    project_id: Text
    actor_id: Text
    operation_id: Text
    request_digest: Text
    workspace_id: Token
    accepted_release_id: Text
    relation_ids: tuple[Text, ...]
    max_bytes: Counter
    manifest_sha256: Text
    manifest_json: str
    target_binding: CatalogBinding
    lifetime_identity: tuple[int, int, str]
    source_workspace_id: Token
    phase: Literal[
        "pinning", "preparing", "exported", "copying", "ready", "failed", "releasing", "released"
    ]
    pins: tuple[CapturePin, ...]
    result_json: str
    estimated_bytes: Counter = 0
    captured_bytes: Counter = 0
    staging_files: tuple[StageFile, ...] = ()
    error_code: Text | None = None


def directory(root: Path, *parts: str) -> int:
    """Create only private task state and traverse every ancestor without symlinks."""
    if not root.is_absolute() or ".." in root.parts:
        raise InputCaptureError("capture_invalid")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in root.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        for part in (".dango", "state", "inputs", *parts):
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if part in {"inputs", *parts}:
                _owned(fd, directory_fd=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def capture_record(root: Path, input_set_id: str) -> Iterator[LockedRecord]:
    """Use the generated ID and persistent lock inode; never unlink a live lock."""
    fd = directory(root, "captures")
    lock = None
    try:
        lock = os.open(
            input_set_id + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        identity = _owned(lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = os.stat(input_set_id + ".lock", dir_fd=fd, follow_symlinks=False)
        if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise InputCaptureError("capture_unavailable")
        yield LockedRecord(fd, input_set_id)
    finally:
        if lock is not None:
            os.close(lock)
        os.close(fd)


def fixture(
    root: Path, context: OperationContext
) -> tuple[AcceptedFixture, str, tuple[int, int, str]]:
    """Require both immutable source metadata and a genuinely held live private lock."""
    fd = directory(root)
    guard = None
    try:
        manifest = LockedRecord(fd, "accepted").read(AcceptedFixture)
        if manifest is None or (manifest.project_id, manifest.actor_id) != (
            context.project_id,
            context.actor_id,
        ):
            raise InputCaptureError("capture_denied")
        digest = hashlib.sha256(manifest.model_dump_json().encode()).hexdigest()
        binding = manifest.binding
        if (binding.project_id, binding.owner_actor_id) != (context.project_id, context.actor_id):
            raise InputCaptureError("capture_denied")
        ids = [r.relation_id for r in manifest.relations]
        names = [(r.schema_name, r.table_name) for r in manifest.relations]
        if (
            len(set(ids)) != len(ids)
            or len(set(names)) != len(names)
            or any(
                not r.columns or fingerprint(r.columns) != r.schema_fingerprint
                for r in manifest.relations
            )
        ):
            raise InputCaptureError("capture_source_unavailable")
        guard = os.open("lifetime.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        identity = _owned(guard)
        raw = os.read(guard, 1048577)
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
        if (
            len(raw) > 1048576
            or type(value) is not dict
            or value.keys() != Guardian.model_fields.keys()
        ):
            raise InputCaptureError("capture_source_unavailable")
        guardian = Guardian.model_validate_json(raw)
        if guardian != Guardian(
            kind="input-fixture-lifetime-v1",
            nonce=manifest.lifetime_nonce,
            project_id=context.project_id,
            actor_id=context.actor_id,
            source_workspace_id=binding.workspace_id,
            source_binding_id=binding.binding_id,
            manifest_sha256=digest,
        ):
            raise InputCaptureError("capture_source_unavailable")
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(guard, fcntl.LOCK_UN)
            raise InputCaptureError("capture_source_unavailable")
        current = os.stat("lifetime.lock", dir_fd=fd, follow_symlinks=False)
        if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise InputCaptureError("capture_source_unavailable")
        return manifest, digest, (identity.st_dev, identity.st_ino, guardian.nonce)
    finally:
        if guard is not None:
            os.close(guard)
        os.close(fd)


def capture_result(value: CaptureRecord) -> InputSet:
    """Validate every saved public identity against its immutable owning capture intent."""
    from dango.storage.inputs import InputSet

    json.loads(value.result_json, object_pairs_hook=_unique_pairs)
    result = InputSet.model_validate_json(value.result_json)
    json.loads(value.manifest_json, object_pairs_hook=_unique_pairs)
    manifest = AcceptedFixture.model_validate_json(value.manifest_json)
    if hashlib.sha256(value.manifest_json.encode()).hexdigest() != value.manifest_sha256:
        raise InputCaptureError("capture_unavailable")
    if (
        result.input_set_id,
        result.project_id,
        result.actor_id,
        result.workspace_id,
        result.accepted_release_id,
    ) != (
        value.input_set_id,
        value.project_id,
        value.actor_id,
        value.workspace_id,
        value.accepted_release_id,
    ) or result.catalog_snapshot_ids != {manifest.catalog_id: str(manifest.snapshot_id)}:
        raise InputCaptureError("capture_unavailable")
    if (manifest.project_id, manifest.actor_id, manifest.binding.workspace_id) != (
        value.project_id,
        value.actor_id,
        value.source_workspace_id,
    ) or (
        value.target_binding.project_id,
        value.target_binding.owner_actor_id,
        value.target_binding.workspace_id,
    ) != (value.project_id, value.actor_id, value.workspace_id):
        raise InputCaptureError("capture_unavailable")
    if (
        manifest.binding.project_id,
        manifest.binding.owner_actor_id,
        manifest.accepted_release_id,
    ) != (
        value.project_id,
        value.actor_id,
        value.accepted_release_id,
    ):
        raise InputCaptureError("capture_unavailable")
    expected_state = (
        "ready"
        if value.phase == "ready"
        else ("failed" if value.phase == "failed" else "preparing")
    )
    if value.phase not in {"releasing", "released"} and result.state != expected_state:
        raise InputCaptureError("capture_unavailable")
    if value.phase == "ready" and (
        value.captured_bytes > value.max_bytes
        or any(
            r.captured_relation_ref.endswith("/pending") for r in result.selected_upstream_relations
        )
    ):
        raise InputCaptureError("capture_unavailable")
    selected = {r.relation_id: r for r in manifest.relations}
    if result.source_version_refs != tuple(
        selected[r].source_version_ref for r in value.relation_ids
    ) or result.checkpoint_refs != tuple(selected[r].checkpoint_ref for r in value.relation_ids):
        raise InputCaptureError("capture_unavailable")
    if tuple(r.relation_id for r in result.selected_upstream_relations) != value.relation_ids:
        raise InputCaptureError("capture_unavailable")
    for relation in result.selected_upstream_relations:
        spec = selected[relation.relation_id]
        if (
            relation.schema_name,
            relation.table_name,
            relation.schema_fingerprint,
            relation.catalog_snapshot_id,
        ) != (
            spec.schema_name,
            spec.table_name,
            spec.schema_fingerprint,
            str(manifest.snapshot_id),
        ) or not relation.captured_relation_ref.startswith(value.target_binding.binding_id + "/"):
            raise InputCaptureError("capture_unavailable")
    if (result.estimated_bytes, result.captured_bytes, result.retention_pins) != (
        value.estimated_bytes,
        value.captured_bytes,
        tuple(p.pin.pin_id for p in value.pins),
    ) or sum(r.bytes for r in result.selected_upstream_relations) != result.captured_bytes:
        raise InputCaptureError("capture_unavailable")
    return result


def result_fields(value: CaptureRecord) -> InputSet:
    """Update a fresh result snapshot at a durable private state transition."""
    from dango.storage.inputs import InputSet

    result = InputSet.model_validate_json(value.result_json)
    return result.model_copy(
        update={
            "retention_pins": tuple(p.pin.pin_id for p in value.pins),
            "estimated_bytes": value.estimated_bytes,
            "captured_bytes": value.captured_bytes,
            "state": "ready"
            if value.phase == "ready"
            else ("failed" if value.phase == "failed" else result.state),
        }
    )


class CaptureAuthority:
    """Keep current fixture/pin joins separate from capture orchestration and public records."""

    root: Path
    allocator: CatalogAllocator
    workspaces: WorkspaceService

    def _owner(self, context: OperationContext, value: CaptureRecord) -> None:
        inspection = self.workspaces._inspect(context, value.operation_id, "inputs.capture")
        if (
            inspection.request_digest != value.request_digest
            or inspection.operation_name != "inputs.capture"
            or inspection.effect is None
        ):
            raise InputCaptureError("capture_denied")
        self.workspaces._check_effect(inspection.effect, "inputs.capture", value.input_set_id)
        if (value.project_id, value.actor_id) != (context.project_id, context.actor_id):
            raise InputCaptureError("capture_denied")
        capture_result(value)

    def _lifetime(self, context: OperationContext, value: CaptureRecord) -> AcceptedFixture:
        manifest, digest, identity = fixture(self.root, context)
        if (digest, identity) != (value.manifest_sha256, value.lifetime_identity):
            raise InputCaptureError("capture_source_unavailable")
        self.allocator._access(context, manifest.binding, "read")
        self.allocator._access(context, value.target_binding, "read")
        return manifest

    def _gate(self, context: OperationContext, value: CaptureRecord) -> AcceptedFixture:
        manifest = self._lifetime(context, value)
        binding, _ = current_binding(self.allocator, context, manifest.binding.workspace_id)
        if binding != manifest.binding:
            raise InputCaptureError("capture_source_unavailable")
        target, _ = current_binding(self.allocator, context, value.workspace_id)
        if target != value.target_binding:
            raise InputCaptureError("capture_recovery_required")
        return manifest

    def _pins(
        self, context: OperationContext, value: CaptureRecord, *, releasing: bool = False
    ) -> None:
        expected = (value.source_workspace_id, value.workspace_id)
        if (not releasing and len(value.pins) != 2) or tuple(
            p.workspace_id for p in value.pins
        ) != expected[: len(value.pins)]:
            raise InputCaptureError("capture_recovery_required")
        for index, item in enumerate(value.pins):
            if (item.pin.operation_id, item.pin.purpose, item.pin.resource_ref) != (
                value.operation_id,
                "input",
                value.input_set_id + (":source" if index == 0 else ":target"),
            ):
                raise InputCaptureError("capture_recovery_required")
        for item in value.pins:
            with self.workspaces._workspace(context, item.workspace_id) as (_, workspace):
                found = next((p for p in workspace.pins if p.pin.pin_id == item.pin.pin_id), None)
                if found is None or found.pin != item.pin or (found.released and not releasing):
                    raise InputCaptureError("capture_recovery_required")
        if not releasing:
            self._gate(context, value)
