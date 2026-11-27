"""dango/storage/inputs.py

Capture one fixed accepted input fixture with strict identities, durable pins and measured copies.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Literal, ParamSpec, TypeVar
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError, model_validator

from dango.operations.contracts import OperationContext, OutcomeReceipt, request_digest
from dango.operations.store import EffectRecord
from dango.storage.catalogs import CatalogAllocator
from dango.storage.input_copy import NativeInputCopy
from dango.storage.input_state import (
    CaptureAuthority,
    CapturePin,
    CaptureRecord,
    InputCaptureError,
    capture_record,
    capture_result,
    fixture,
    result_fields,
)
from dango.storage.workspace_records import Counter, Record, Stamp, Text, Token
from dango.storage.workspace_state import current_binding
from dango.storage.workspaces import WorkspaceService

_P = ParamSpec("_P")
_R = TypeVar("_R")
_EFFECT = "inputs.capture"


class CapturedRelation(Record):
    """Reference a materialized original qualified relation and its accepted schema/snapshot."""

    relation_id: Text
    schema_name: Text
    table_name: Text
    schema_fingerprint: Text
    catalog_snapshot_id: Text
    captured_relation_ref: Text
    bytes: Counter


class InputSet(Record):
    """Keep the exact fixed public capture shape; fresh nested snapshots isolate callers."""

    input_set_id: Token
    project_id: Text
    actor_id: Text
    workspace_id: Token
    accepted_release_id: Text
    catalog_snapshot_ids: dict[str, str]
    selected_upstream_relations: tuple[CapturedRelation, ...]
    source_version_refs: tuple[Text, ...]
    checkpoint_refs: tuple[Text, ...]
    capture_method: Literal["materialized_selected_snapshot"]
    estimated_bytes: Counter
    captured_bytes: Counter
    created_at: Stamp
    retention_pins: tuple[Text, ...]
    state: Literal["preparing", "ready", "failed"]

    @model_validator(mode="after")
    def consistent(self) -> InputSet:
        """Reject empty/duplicate relation identities and invalid nested snapshot references."""
        relations = self.selected_upstream_relations
        if (
            not relations
            or len({r.relation_id for r in relations}) != len(relations)
            or not self.catalog_snapshot_ids
        ):
            raise ValueError("Supply unique admitted input identities.")
        if len(self.source_version_refs) != len(relations) or len(self.checkpoint_refs) != len(
            relations
        ):
            raise ValueError("Supply one source/checkpoint reference per selected relation.")
        for key, value in self.catalog_snapshot_ids.items():
            TypeAdapter(Text).validate_python(key, strict=True)
            TypeAdapter(Text).validate_python(value, strict=True)
        return self


def _safe(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def call(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Hide native, filesystem and validation diagnostics behind fixed capture errors."""
        try:
            return method(*args, **kwargs)
        except InputCaptureError:
            raise
        except (ValidationError, TypeError):
            raise InputCaptureError("capture_invalid") from None
        except Exception:
            raise InputCaptureError("capture_unavailable") from None

    return call


class InputCaptureService(CaptureAuthority):
    """Require admitted operations and current source/target authority; context is not authentication."""

    def __init__(
        self,
        project_root: Path,
        allocator: CatalogAllocator,
        workspaces: WorkspaceService,
        clock: Callable[[], datetime],
    ):
        self.root, self.allocator, self.workspaces, self.clock = (
            project_root,
            allocator,
            workspaces,
            clock,
        )
        self.store = workspaces.store
        self.copy = NativeInputCopy(project_root, allocator)

    def _now(self) -> str:
        value = self.clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise InputCaptureError("capture_invalid")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def _effect(
        self, context: OperationContext, input_set_id: str, state: Literal["prepared", "applied"]
    ) -> None:
        self.store.record_effect(
            context,
            context.operation_id,
            EffectRecord(
                effect_id=_EFFECT,
                name=_EFFECT,
                sequence=0,
                state=state,
                native_ref=input_set_id,
            ),
        )

    @_safe
    def capture_inputs(
        self,
        context: OperationContext,
        workspace_id: str,
        accepted_release_id: str,
        relation_ids: tuple[str, ...],
        max_bytes: int,
    ) -> InputSet:
        """Materialize exactly the admitted selected snapshot with durable original-operation replay."""
        TypeAdapter(Token).validate_python(workspace_id, strict=True)
        TypeAdapter(Counter).validate_python(max_bytes, strict=True)
        TypeAdapter(tuple[Text, ...]).validate_python(relation_ids, strict=True)
        if not relation_ids or len(set(relation_ids)) != len(relation_ids):
            raise InputCaptureError("capture_invalid")
        inspection, request = self.workspaces._admit(
            context,
            _EFFECT,
            {
                "workspace_id": workspace_id,
                "accepted_release_id": accepted_release_id,
                "relation_ids": list(relation_ids),
                "max_bytes": max_bytes,
            },
        )
        with self.workspaces._operation(context, request, workspace_id):
            inspection = self.workspaces._inspect(context, context.operation_id, _EFFECT)
            effect = inspection.effect
            if effect is None:
                if inspection.state == "finalized":
                    raise InputCaptureError("capture_recovery_required")
                input_set_id = uuid4().hex
                self._effect(context, input_set_id, "prepared")
            else:
                input_set_id = self.workspaces._check_effect(effect, _EFFECT)
            with capture_record(self.root, input_set_id) as record:
                value = record.read(CaptureRecord)
                inspection = self.workspaces._inspect(context, context.operation_id, _EFFECT)
                if value is None:
                    if effect is not None or inspection.state == "finalized":
                        raise InputCaptureError("capture_recovery_required")
                    manifest, digest, identity = fixture(self.root, context)
                    if (
                        accepted_release_id != manifest.accepted_release_id
                        or workspace_id == manifest.binding.workspace_id
                    ):
                        raise InputCaptureError("capture_denied")
                    selected = {r.relation_id: r for r in manifest.relations}
                    if any(r not in selected for r in relation_ids):
                        raise InputCaptureError("capture_denied")
                    self.workspaces.get(context, workspace_id)
                    target, _ = current_binding(self.allocator, context, workspace_id)
                    result = InputSet(
                        input_set_id=input_set_id,
                        project_id=context.project_id,
                        actor_id=context.actor_id,
                        workspace_id=workspace_id,
                        accepted_release_id=accepted_release_id,
                        catalog_snapshot_ids={manifest.catalog_id: str(manifest.snapshot_id)},
                        selected_upstream_relations=tuple(
                            CapturedRelation(
                                relation_id=r,
                                schema_name=selected[r].schema_name,
                                table_name=selected[r].table_name,
                                schema_fingerprint=selected[r].schema_fingerprint,
                                catalog_snapshot_id=str(manifest.snapshot_id),
                                captured_relation_ref=target.binding_id + "/pending",
                                bytes=0,
                            )
                            for r in relation_ids
                        ),
                        source_version_refs=tuple(
                            selected[r].source_version_ref for r in relation_ids
                        ),
                        checkpoint_refs=tuple(selected[r].checkpoint_ref for r in relation_ids),
                        capture_method="materialized_selected_snapshot",
                        estimated_bytes=0,
                        captured_bytes=0,
                        created_at=self._now(),
                        retention_pins=(),
                        state="preparing",
                    )
                    value = CaptureRecord(
                        input_set_id=input_set_id,
                        project_id=context.project_id,
                        actor_id=context.actor_id,
                        operation_id=context.operation_id,
                        request_digest=request_digest(context, request),
                        workspace_id=workspace_id,
                        accepted_release_id=accepted_release_id,
                        relation_ids=relation_ids,
                        max_bytes=max_bytes,
                        manifest_sha256=digest,
                        manifest_json=manifest.model_dump_json(),
                        target_binding=target,
                        lifetime_identity=identity,
                        source_workspace_id=manifest.binding.workspace_id,
                        phase="pinning",
                        pins=(),
                        result_json=result.model_dump_json(),
                    )
                    record.save(value)
                self._owner(context, value, input_set_id)
                if (value.operation_id, value.request_digest) != (
                    context.operation_id,
                    request_digest(context, request),
                ):
                    raise InputCaptureError("capture_denied")
                if inspection.state == "finalized":
                    if inspection.receipt is None or inspection.receipt.result_ref != input_set_id:
                        raise InputCaptureError("capture_recovery_required")
                    if inspection.receipt.status == "success":
                        return capture_result(value)
                    raise InputCaptureError(value.error_code or "capture_released")

                def update(**changes: object) -> None:
                    """Publish one durable private transition and its immutable result snapshot."""
                    nonlocal value
                    assert value is not None
                    value = value.model_copy(update=changes)
                    result = result_fields(value)
                    value = value.model_copy(update={"result_json": result.model_dump_json()})
                    record.save(value)

                def gate() -> None:
                    """Check live lifetime/access without reversing the held allocator lock order."""
                    assert value is not None
                    self._lifetime(context, value)

                if value.phase == "ready":
                    self._pins(context, value)
                    self._effect(context, input_set_id, "applied")
                    self.store.finalize_operation(
                        context,
                        OutcomeReceipt(
                            operation_id=context.operation_id,
                            status="success",
                            result_ref=input_set_id,
                        ),
                    )
                    return capture_result(value)
                if value.phase == "failed":
                    self.store.finalize_operation(
                        context,
                        OutcomeReceipt(
                            operation_id=context.operation_id,
                            status="failed",
                            result_ref=input_set_id,
                            error_code=value.error_code,
                            action="Inspect retained owned input effects before release or a new capture.",
                        ),
                    )
                    raise InputCaptureError(value.error_code or "capture_recovery_required")
                try:
                    if value.phase == "pinning":
                        for index, workspace in enumerate(
                            (value.source_workspace_id, value.workspace_id)
                        ):
                            if index >= len(value.pins):
                                pin = self.workspaces.acquire_pin(
                                    context,
                                    workspace,
                                    "input",
                                    input_set_id + (":source" if index == 0 else ":target"),
                                )
                                update(
                                    pins=(*value.pins, CapturePin(workspace_id=workspace, pin=pin))
                                )
                        update(phase="preparing")
                    self._pins(context, value)
                    manifest = self._gate(context, value)
                    selected = {r.relation_id: r for r in manifest.relations}
                    relations = tuple(selected[r] for r in relation_ids)
                    if value.phase != "preparing":
                        raise InputCaptureError("capture_recovery_required")
                    self.copy.preflight(context, value.target_binding, relations)
                    estimate = self.copy.export(
                        context,
                        manifest,
                        relations,
                        input_set_id,
                        max_bytes,
                        gate,
                        lambda size: update(estimated_bytes=size),
                    )
                    update(
                        phase="exported",
                        estimated_bytes=estimate,
                        staging_files=self.copy.inventory(input_set_id, len(relations)),
                    )
                    captured = list(capture_result(value).selected_upstream_relations)
                    for index, relation in enumerate(relations):
                        update(phase="copying")
                        ref, size = self.copy.copy_one(
                            context, value.target_binding, relation, input_set_id, index
                        )
                        captured[index] = captured[index].model_copy(
                            update={
                                "captured_relation_ref": value.target_binding.binding_id
                                + "/"
                                + ref,
                                "bytes": size,
                            }
                        )
                        result = capture_result(value).model_copy(
                            update={
                                "selected_upstream_relations": tuple(captured),
                                "captured_bytes": sum(r.bytes for r in captured),
                            }
                        )
                        value = value.model_copy(
                            update={
                                "captured_bytes": result.captured_bytes,
                                "result_json": result.model_dump_json(),
                            }
                        )
                        record.save(value)
                        if value.captured_bytes > max_bytes:
                            raise InputCaptureError("capture_quota_exceeded")
                    self._pins(context, value)
                    update(phase="ready")
                    self._effect(context, input_set_id, "applied")
                    self.store.finalize_operation(
                        context,
                        OutcomeReceipt(
                            operation_id=context.operation_id,
                            status="success",
                            result_ref=input_set_id,
                        ),
                    )
                    return capture_result(value)
                except Exception as error:
                    if value.phase == "ready":
                        raise InputCaptureError("capture_recovery_required") from None
                    code = (
                        error.code
                        if isinstance(error, InputCaptureError)
                        else "capture_recovery_required"
                    )
                    update(phase="failed", error_code=code)
                    self.store.finalize_operation(
                        context,
                        OutcomeReceipt(
                            operation_id=context.operation_id,
                            status="failed",
                            result_ref=input_set_id,
                            error_code=code,
                            action="Inspect retained owned input effects before release or a new capture.",
                        ),
                    )
                    raise InputCaptureError(code) from None

    @_safe
    def inspect_capture(self, context: OperationContext, input_set_id: str) -> InputSet:
        """Return only currently retained consistent capture state, never released historical authority."""
        TypeAdapter(Token).validate_python(input_set_id, strict=True)
        with capture_record(self.root, input_set_id) as record:
            value = record.read(CaptureRecord)
            if value is None:
                raise InputCaptureError("capture_denied")
            status = self._owner(context, value, input_set_id)
            if value.phase in {"releasing", "released"}:
                raise InputCaptureError("capture_released")
            self._pins(context, value)
            if value.phase == "ready":
                self._success(value, status)
            return capture_result(value)

    @_safe
    def release_capture(self, context: OperationContext, input_set_id: str) -> None:
        """Release only original capture pins; cleanup works after finalization or lifetime loss."""
        TypeAdapter(Token).validate_python(input_set_id, strict=True)
        with capture_record(self.root, input_set_id) as record:
            value = record.read(CaptureRecord)
            if value is None:
                raise InputCaptureError("capture_denied")
            self._owner(context, value, input_set_id)
            if context.operation_id != value.operation_id:
                raise InputCaptureError("capture_denied")
            if value.phase == "released":
                return
            self._pins(context, value, releasing=True)
            value = value.model_copy(update={"phase": "releasing"})
            record.save(value)
            for item in value.pins:
                self.workspaces.release_pin(context, item.workspace_id, item.pin.pin_id)
            self.copy.cleanup(input_set_id, value.staging_files)
            status = self.workspaces._inspect(context, value.operation_id, _EFFECT)
            if status.state != "finalized":
                self.store.finalize_operation(
                    context,
                    OutcomeReceipt(
                        operation_id=context.operation_id,
                        status="cancelled",
                        result_ref=input_set_id,
                        error_code="capture_released",
                        action="Admit a new capture before using released historical input metadata.",
                    ),
                )
            record.save(value.model_copy(update={"phase": "released"}))
