"""dango/storage/workspaces.py

Join admitted operations, private workspace leases/pins and owned catalog recovery.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Literal, ParamSpec, TypeVar
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from dango.operations.contracts import (
    OperationContext,
    OutcomeReceipt,
)
from dango.operations.store import OperationStore
from dango.storage.catalogs import CatalogAllocator, CatalogError
from dango.storage.workspace_records import (
    Change,
    Counter,
    PinState,
    Purpose,
    Text,
    Token,
    Workspace,
    WorkspaceError,
    WorkspacePin,
    WorkspaceState,
)
from dango.storage.workspace_state import WorkspaceJournal, current_binding, locked

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _safe(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def call(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Keep private IO and native exception diagnostics behind fixed public codes."""
        try:
            return method(*args, **kwargs)
        except WorkspaceError:
            raise
        except CatalogError:
            raise WorkspaceError("workspace_recovery_required") from None
        except (ValidationError, TypeError):
            raise WorkspaceError("invalid_input") from None
        except ValueError as error:
            code = (
                "workspace_denied"
                if getattr(error, "code", None) == "operation_denied"
                else "workspace_unavailable"
            )
            raise WorkspaceError(code) from None
        except Exception:
            raise WorkspaceError("workspace_unavailable") from None

    return call


class WorkspaceService(WorkspaceJournal):
    """Coordinate this service's callers; the server still owns authentication and grants."""

    def __init__(
        self,
        project_root: Path,
        allocator: CatalogAllocator,
        store: OperationStore,
        clock: Callable[[], datetime],
    ):
        self.root, self.allocator, self.store, self.clock = project_root, allocator, store, clock

    def _now(self) -> datetime:
        return self._utc(self.clock())

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise WorkspaceError("invalid_input")
        return value.astimezone(timezone.utc)

    @_safe
    def allocate(
        self,
        context: OperationContext,
        branch_ref: str,
        code_commit: str,
        location: Literal["remote", "local"],
    ) -> Workspace:
        """Persist a generated ID before native effects and resume only its canonical operation."""
        branch_ref = TypeAdapter(Text).validate_python(branch_ref, strict=True)
        code_commit = TypeAdapter(Text).validate_python(code_commit, strict=True)
        TypeAdapter(Literal["remote", "local"]).validate_python(location, strict=True)
        _, request = self._admit(
            context,
            "workspace.allocate",
            {"branch_ref": branch_ref, "code_commit": code_commit, "location": location},
        )
        with self._operation(context, request, None) as (operation, intent):
            inspection = self._inspect(context, context.operation_id, request.operation_name)
            effect = inspection.effect
            if inspection.state == "finalized":
                if (
                    effect is None
                    or effect.state != "applied"
                    or intent.result is None
                    or inspection.receipt is None
                    or inspection.receipt.status != "success"
                ):
                    raise WorkspaceError("workspace_recovery_required")
                self._check_effect(effect, request.operation_name, intent.result.workspace_id)
                if inspection.receipt != OutcomeReceipt(
                    operation_id=context.operation_id,
                    status="success",
                    result_ref=intent.result.workspace_id,
                    resource_revision=str(intent.result.revision),
                ):
                    raise WorkspaceError("workspace_recovery_required")
                return intent.result
            if effect is None:
                try:
                    self._effect(context, request.operation_name, uuid4().hex, "prepared")
                except ValueError as error:
                    if getattr(error, "code", None) != "effect_conflict":
                        raise
                effect = self._inspect(context, context.operation_id, request.operation_name).effect
            if effect is None:
                raise WorkspaceError("workspace_recovery_required")
            workspace_id = self._check_effect(effect, request.operation_name, intent.workspace_id)
            if intent.workspace_id is None:
                intent = intent.model_copy(update={"workspace_id": workspace_id, "phase": "bound"})
                operation.save(intent)
            with locked(self.root, "resources", (context.project_id, workspace_id)) as record:
                value = record.read(WorkspaceState)
                if value is None:
                    if intent.phase not in {"intent", "bound"}:
                        raise WorkspaceError("workspace_recovery_required")
                    value = WorkspaceState(
                        workspace_id=workspace_id,
                        project_id=context.project_id,
                        actor_id=context.actor_id,
                        allocation_operation_id=context.operation_id,
                        phase="allocating",
                        workspace=None,
                        binding=None,
                        operations=(context.operation_id,),
                        pins=(),
                        changes=(),
                    )
                    record.save(value)
                self._consistent(value)
                if (value.allocation_operation_id, value.project_id, value.actor_id) != (
                    context.operation_id,
                    context.project_id,
                    context.actor_id,
                ):
                    raise WorkspaceError("workspace_denied")
                if intent.phase in {"intent", "bound"}:
                    intent = intent.model_copy(update={"phase": "prepared"})
                    operation.save(intent)
                if value.workspace is None:
                    now = self._now()
                    binding = self.allocator.allocate(context, workspace_id)
                    if binding.state != "ready":
                        binding = self.allocator.reconcile(context, binding)
                    workspace = Workspace(
                        workspace_id=workspace_id,
                        project_id=context.project_id,
                        actor_id=context.actor_id,
                        branch_ref=branch_ref,
                        code_commit=code_commit,
                        execution_location=location,
                        catalog_binding_ref=binding.binding_id,
                        data_root_ref=binding.data_root_ref,
                        created_at=now.isoformat(timespec="microseconds"),
                        lease_expiry=(now + timedelta(days=7)).isoformat(timespec="microseconds"),
                        state="ready",
                        revision=0,
                    )
                    value = value.model_copy(
                        update={"phase": "ready", "binding": binding, "workspace": workspace}
                    )
                    record.save(value)
                else:
                    workspace = value.workspace
                    binding, phase = current_binding(self.allocator, context, workspace_id)
                    if phase != "prepared" or binding.state != "ready":
                        raise WorkspaceError("workspace_recovery_required")
                    self.allocator.reconcile(context, binding)
                self._effect(context, request.operation_name, workspace_id, "applied")
                operation.save(intent.model_copy(update={"phase": "applied", "result": workspace}))
                self.store.finalize_operation(
                    context,
                    OutcomeReceipt(
                        operation_id=context.operation_id,
                        status="success",
                        result_ref=workspace_id,
                        resource_revision=str(workspace.revision),
                    ),
                )
                return workspace

    @_safe
    def get(self, context: OperationContext, workspace_id: str) -> Workspace:
        """Read current owner state, refusing to expose an unapplied allocation as ready."""
        self._inspect(context, context.operation_id, "workspace.activity")
        with self._workspace(context, workspace_id) as (_, value):
            allocation = self._inspect(context, value.allocation_operation_id, "workspace.allocate")
            if (
                value.workspace is None
                or allocation.effect is None
                or allocation.effect.state != "applied"
            ):
                raise WorkspaceError("workspace_recovery_required")
            self._check_effect(allocation.effect, "workspace.allocate", workspace_id)
            return value.workspace

    @_safe
    def extend(
        self, context: OperationContext, workspace_id: str, expected_revision: int, expiry: datetime
    ) -> Workspace:
        """CAS a longer lease and retain its exact result atomically for interrupted replay."""
        TypeAdapter(Counter).validate_python(expected_revision, strict=True)
        expiry = self._utc(expiry)
        inspection, request = self._admit(
            context,
            "workspace.extend",
            {"workspace_id": workspace_id, "expiry": expiry.isoformat(timespec="microseconds")},
            expected_revision,
        )
        with self._operation(context, request, workspace_id) as (operation, intent):
            with self._workspace(context, workspace_id) as (record, value):
                change = self._change(value, context, intent)
                if inspection.state == "finalized":
                    if change is None or inspection.receipt != change.receipt:
                        raise WorkspaceError("workspace_recovery_required")
                    return change.workspace
                if change is not None:
                    return self._complete(context, operation, intent, change)
                workspace = value.workspace
                if (
                    workspace is None
                    or value.phase != "ready"
                    or workspace.revision != expected_revision
                ):
                    raise WorkspaceError("workspace_conflict")
                if expiry <= self._now() or expiry <= datetime.fromisoformat(
                    workspace.lease_expiry
                ):
                    raise WorkspaceError("invalid_input")
                self._effect(context, request.operation_name, workspace_id, "prepared")
                workspace = workspace.model_copy(
                    update={
                        "lease_expiry": expiry.isoformat(timespec="microseconds"),
                        "revision": workspace.revision + 1,
                    }
                )
                change = Change(
                    operation_id=context.operation_id,
                    request_digest=intent.request_digest,
                    workspace=workspace,
                    receipt=OutcomeReceipt(
                        operation_id=context.operation_id,
                        status="success",
                        result_ref=workspace_id,
                        resource_revision=str(workspace.revision),
                    ),
                )
                record.save(
                    value.model_copy(
                        update={
                            "workspace": workspace,
                            "operations": (*value.operations, context.operation_id),
                            "changes": (*value.changes, change),
                        }
                    )
                )
                return self._complete(context, operation, intent, change)

    def _retained(self, context: OperationContext, value: WorkspaceState) -> bool:
        active = False
        for operation in value.operations:
            status = self._inspect(context, operation, "workspace.activity")
            active |= operation != context.operation_id and (
                status.state != "finalized"
                or status.receipt is None
                or status.receipt.status == "recovery_required"
            )
        return (
            active
            or any(not p.released for p in value.pins)
            or (value.workspace is not None and value.workspace.state == "building")
        )

    @_safe
    def expire(
        self, context: OperationContext, workspace_id: str, expected_revision: int
    ) -> OutcomeReceipt:
        """Remove only an explicitly eligible workspace; incomplete effects remain resumable."""
        TypeAdapter(Counter).validate_python(expected_revision, strict=True)
        inspection, request = self._admit(
            context, "workspace.expire", {"workspace_id": workspace_id}, expected_revision
        )
        if inspection.state == "finalized":
            if inspection.receipt is None or inspection.receipt.result_ref != workspace_id:
                raise WorkspaceError("workspace_recovery_required")
            return inspection.receipt
        with self._operation(context, request, workspace_id) as (operation, intent):
            with self._workspace(context, workspace_id) as (record, value):
                change = self._change(value, context, intent)
                if change is not None:
                    # Still authorize an already-removed native binding before success replay.
                    binding, _ = current_binding(self.allocator, context, workspace_id)
                    self.allocator.remove_owned(context, binding)
                    self._complete(context, operation, intent, change)
                    return change.receipt
                workspace = value.workspace
                resuming = (
                    value.phase == "removing"
                    and value.removal_operation_id == context.operation_id
                    and value.removal_revision == expected_revision
                )
                if workspace is None or (
                    not resuming
                    and (value.phase != "ready" or workspace.revision != expected_revision)
                ):
                    raise WorkspaceError("workspace_conflict")
                reason = (
                    "workspace_lease_active"
                    if datetime.fromisoformat(workspace.lease_expiry) > self._now()
                    else ("workspace_retained" if self._retained(context, value) else None)
                )
                if reason is not None:
                    return self.store.finalize_operation(
                        context,
                        OutcomeReceipt(
                            operation_id=context.operation_id,
                            status="blocked",
                            result_ref=workspace_id,
                            resource_revision=str(workspace.revision),
                            error_code=reason,
                            action="Wait for the lease and owning operations or release retention pins before a new expiry operation.",
                        ),
                    )
                self._effect(context, request.operation_name, workspace_id, "prepared")
                if not resuming:
                    workspace = workspace.model_copy(
                        update={"state": "recovery_required", "revision": workspace.revision + 1}
                    )
                    value = value.model_copy(
                        update={
                            "workspace": workspace,
                            "phase": "removing",
                            "removal_operation_id": context.operation_id,
                            "removal_revision": expected_revision,
                            "operations": (*value.operations, context.operation_id),
                        }
                    )
                    record.save(value)
                if self._retained(context, value):
                    raise WorkspaceError("workspace_recovery_required")
                binding, _ = current_binding(self.allocator, context, workspace_id)
                self.allocator.remove_owned(context, binding)
                binding, phase = current_binding(self.allocator, context, workspace_id)
                if phase != "removed":
                    raise WorkspaceError("workspace_recovery_required")
                workspace = workspace.model_copy(
                    update={"state": "expired", "revision": workspace.revision + 1}
                )
                receipt = OutcomeReceipt(
                    operation_id=context.operation_id,
                    status="success",
                    result_ref=workspace_id,
                    resource_revision=str(workspace.revision),
                )
                change = Change(
                    operation_id=context.operation_id,
                    request_digest=intent.request_digest,
                    workspace=workspace,
                    receipt=receipt,
                )
                record.save(
                    value.model_copy(
                        update={
                            "phase": "expired",
                            "binding": binding,
                            "workspace": workspace,
                            "changes": (*value.changes, change),
                        }
                    )
                )
                self._complete(context, operation, intent, change)
                return receipt

    @_safe
    def acquire_pin(
        self, context: OperationContext, workspace_id: str, purpose: Purpose, resource_ref: str
    ) -> WorkspacePin:
        """Retain active input/build/query/backup use independently of the UI lease."""
        TypeAdapter(Purpose).validate_python(purpose, strict=True)
        TypeAdapter(Text).validate_python(resource_ref, strict=True)
        if self._inspect(context, context.operation_id, "workspace.activity").state == "finalized":
            raise WorkspaceError("workspace_denied")
        with self._workspace(context, workspace_id) as (record, value):
            if value.phase != "ready":
                raise WorkspaceError("workspace_conflict")
            for entry in value.pins:
                if (entry.pin.operation_id, entry.pin.purpose, entry.pin.resource_ref) == (
                    context.operation_id,
                    purpose,
                    resource_ref,
                ):
                    if entry.released:
                        raise WorkspaceError("workspace_conflict")
                    return entry.pin
            pin = WorkspacePin(
                pin_id=uuid4().hex,
                operation_id=context.operation_id,
                purpose=purpose,
                resource_ref=resource_ref,
            )
            operations = (
                value.operations
                if context.operation_id in value.operations
                else (*value.operations, context.operation_id)
            )
            record.save(
                value.model_copy(
                    update={
                        "pins": (*value.pins, PinState(pin=pin, released=False)),
                        "operations": operations,
                    }
                )
            )
            return pin

    @_safe
    def release_pin(self, context: OperationContext, workspace_id: str, pin_id: str) -> None:
        """Release only the recorded owning operation, including finalized cleanup retries."""
        TypeAdapter(Token).validate_python(pin_id, strict=True)
        self._inspect(context, context.operation_id, "workspace.activity")
        with self._workspace(context, workspace_id) as (record, value):
            entry = next((p for p in value.pins if p.pin.pin_id == pin_id), None)
            if entry is None or entry.pin.operation_id != context.operation_id:
                raise WorkspaceError("workspace_denied")
            pins = tuple(
                p.model_copy(update={"released": True}) if p == entry else p for p in value.pins
            )
            record.save(value.model_copy(update={"pins": pins}))

    @_safe
    def has_pins(self, context: OperationContext, workspace_id: str) -> bool:
        """Refuse unknown operation or pin state rather than reporting unretained data."""
        self._inspect(context, context.operation_id, "workspace.activity")
        with self._workspace(context, workspace_id) as (_, value):
            for entry in value.pins:
                self._inspect(context, entry.pin.operation_id, "workspace.activity")
            return any(not p.released for p in value.pins)
