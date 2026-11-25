"""dango/storage/workspace_state.py

Lock and durably replace private workspace manifests without following filesystem links.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, TypeAdapter

from dango.operations.contracts import (
    OperationContext,
    OperationRequest,
    canonical_request_bytes,
    request_digest,
)
from dango.operations.store import EffectInspection, EffectRecord, OperationStore
from dango.storage.catalog_ownership import OwnedWorkspace
from dango.storage.catalogs import CatalogAllocator, CatalogBinding
from dango.storage.workspace_records import (
    Change,
    OperationIntent,
    Token,
    Workspace,
    WorkspaceError,
    WorkspaceState,
)

_Model = TypeVar("_Model", bound=BaseModel)
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_LIMIT = 1048576


def _owned(fd: int, *, directory_fd: bool = False) -> os.stat_result:
    info = os.fstat(fd)
    kind = stat.S_ISDIR if directory_fd else stat.S_ISREG
    mode = 0o700 if directory_fd else 0o600
    if (
        not kind(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != mode
        or (not directory_fd and info.st_nlink != 1)
    ):
        raise WorkspaceError("workspace_unavailable")
    return info


def _root(project_root: Path, category: str) -> int:
    # Existing ancestors need not be private0700, but no component may be a symlink.
    if not project_root.is_absolute() or ".." in project_root.parts:
        raise WorkspaceError("invalid_input")
    fd = os.open("/", _READ | os.O_DIRECTORY)
    try:
        for part in project_root.parts[1:]:
            child = os.open(part, _READ | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        for part in (".dango", "state", "workspaces", category):
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            child = os.open(part, _READ | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            if part in {"workspaces", category}:
                _owned(fd, directory_fd=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


class LockedRecord:
    """Read and replace one record while its persistent kernel lock is held."""

    def __init__(self, directory_fd: int, key: str):
        self.fd, self.key = directory_fd, key

    def read(self, cls: type[_Model]) -> _Model | None:
        """Return absence only for a missing file, never malformed private state."""
        try:
            fd = os.open(self.key + ".json", _READ, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            _owned(stream.fileno())
            payload = stream.read(_LIMIT + 1)
        if len(payload) > _LIMIT:
            raise WorkspaceError("workspace_unavailable")
        # JSON mode admits immutable tuples while retaining scalar strictness.
        return cls.model_validate_json(payload)

    def save(self, value: BaseModel) -> None:
        """Fsync a complete validated snapshot before replacing its prior generation."""
        value = type(value).model_validate(value.model_dump())
        payload = value.model_dump_json().encode()
        if len(payload) > _LIMIT:
            raise WorkspaceError("workspace_unavailable")
        temporary = uuid4().hex + ".pending"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Refuse substituted/special destination files rather than overwriting them.
            try:
                old = os.open(self.key + ".json", _READ, dir_fd=self.fd)
            except FileNotFoundError:
                pass
            else:
                try:
                    _owned(old)
                finally:
                    os.close(old)
            os.replace(temporary, self.key + ".json", src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass


@contextmanager
def locked(project_root: Path, category: str, identity: tuple[str, ...]) -> Iterator[LockedRecord]:
    """Use stable hashed names and refuse lock-path substitution before releasing ownership."""
    if category not in {"operations", "resources"}:
        raise WorkspaceError("invalid_input")
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    fd = _root(project_root, category)
    lock = None
    try:
        lock = os.open(key + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        info = _owned(lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = os.stat(key + ".lock", dir_fd=fd, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise WorkspaceError("workspace_unavailable")
        yield LockedRecord(fd, key)
    finally:
        if lock is not None:
            os.close(lock)
        os.close(fd)


def current_binding(
    allocator: CatalogAllocator, context: OperationContext, workspace_id: str
) -> tuple[CatalogBinding, str]:
    """Read T04A's current journal; callers still invoke its public authorized operation."""
    with OwnedWorkspace(
        allocator.state_root, allocator.data_root, context.project_id, workspace_id
    ).locked() as owned:
        if owned.record is None:
            raise WorkspaceError("workspace_recovery_required")
        binding = CatalogBinding.model_validate(owned.record["binding"])
        if (
            binding.project_id != context.project_id
            or binding.workspace_id != workspace_id
            or binding.owner_actor_id != context.actor_id
        ):
            raise WorkspaceError("workspace_denied")
        return binding, owned.record["phase"]


class WorkspaceJournal:
    """Join owner-scoped admission and private intent under the documented lock order."""

    root: Path
    store: OperationStore

    def _inspect(self, context: OperationContext, target: str, effect: str) -> EffectInspection:
        if type(context) is not OperationContext:
            raise WorkspaceError("invalid_input")
        context = OperationContext.model_validate(context.model_dump())
        return self.store.inspect_effect(context, target, effect)

    def _admit(
        self,
        context: OperationContext,
        name: str,
        payload: dict[str, object],
        revision: int | None = None,
    ) -> tuple[EffectInspection, OperationRequest]:
        request = OperationRequest(
            operation_name=name,
            payload=payload,
            expected_revision=None if revision is None else str(revision),
        )
        inspection = self._inspect(context, context.operation_id, name)
        if inspection.operation_name != name or inspection.request_digest != request_digest(
            context, request
        ):
            raise WorkspaceError("workspace_denied")
        return inspection, request

    @contextmanager
    def _operation(
        self,
        context: OperationContext,
        request: OperationRequest,
        workspace_id: str | None,
    ) -> Iterator[tuple[LockedRecord, OperationIntent]]:
        with locked(
            self.root, "operations", (context.project_id, context.actor_id, context.operation_id)
        ) as record:
            value = record.read(OperationIntent)
            digest = request_digest(context, request)
            parameters = canonical_request_bytes(request).decode()
            inspection = self._inspect(context, context.operation_id, request.operation_name)
            if value is None:
                if inspection.effect is not None or inspection.state == "finalized":
                    raise WorkspaceError("workspace_recovery_required")
                value = OperationIntent(
                    operation_id=context.operation_id,
                    project_id=context.project_id,
                    actor_id=context.actor_id,
                    name=request.operation_name,
                    request_digest=digest,
                    parameters_json=parameters,
                    phase="intent",
                    workspace_id=workspace_id,
                )
                record.save(value)
            if (
                value.operation_id,
                value.project_id,
                value.actor_id,
                value.name,
                value.request_digest,
                value.parameters_json,
            ) != (
                context.operation_id,
                context.project_id,
                context.actor_id,
                request.operation_name,
                digest,
                parameters,
            ) or (workspace_id is not None and value.workspace_id != workspace_id):
                raise WorkspaceError("workspace_denied")
            yield record, value

    @contextmanager
    def _workspace(
        self, context: OperationContext, workspace_id: str
    ) -> Iterator[tuple[LockedRecord, WorkspaceState]]:
        TypeAdapter(Token).validate_python(workspace_id, strict=True)
        with locked(self.root, "resources", (context.project_id, workspace_id)) as record:
            value = record.read(WorkspaceState)
            if value is None or (value.workspace_id, value.project_id, value.actor_id) != (
                workspace_id,
                context.project_id,
                context.actor_id,
            ):
                raise WorkspaceError("workspace_denied")
            self._consistent(value)
            yield record, value

    @staticmethod
    def _consistent(value: WorkspaceState) -> None:
        if (
            len(set(value.operations)) != len(value.operations)
            or value.allocation_operation_id not in value.operations
        ):
            raise WorkspaceError("workspace_unavailable")
        if len({p.pin.pin_id for p in value.pins}) != len(value.pins) or any(
            p.pin.operation_id not in value.operations for p in value.pins
        ):
            raise WorkspaceError("workspace_unavailable")
        if value.workspace is not None and (
            value.workspace.workspace_id,
            value.workspace.project_id,
            value.workspace.actor_id,
        ) != (value.workspace_id, value.project_id, value.actor_id):
            raise WorkspaceError("workspace_unavailable")
        if value.binding is not None and (
            value.binding.workspace_id,
            value.binding.project_id,
            value.binding.owner_actor_id,
        ) != (value.workspace_id, value.project_id, value.actor_id):
            raise WorkspaceError("workspace_unavailable")
        if value.workspace is not None and (
            value.binding is None
            or value.workspace.catalog_binding_ref != value.binding.binding_id
            or value.workspace.data_root_ref != value.binding.data_root_ref
        ):
            raise WorkspaceError("workspace_unavailable")
        if len({c.operation_id for c in value.changes}) != len(value.changes) or any(
            c.operation_id not in value.operations
            or c.receipt.operation_id != c.operation_id
            or c.receipt.result_ref != value.workspace_id
            or c.receipt.resource_revision != str(c.workspace.revision)
            or c.workspace.workspace_id != value.workspace_id
            for c in value.changes
        ):
            raise WorkspaceError("workspace_unavailable")

    def _effect(
        self,
        context: OperationContext,
        name: str,
        workspace_id: str,
        state: Literal["prepared", "applied"],
    ) -> None:
        self.store.record_effect(
            context,
            context.operation_id,
            EffectRecord(
                effect_id=name,
                sequence=0,
                name=name,
                state=state,
                native_ref=workspace_id,
            ),
        )

    @staticmethod
    def _check_effect(value: EffectRecord, name: str, workspace_id: str | None = None) -> str:
        if (
            value.name != name
            or value.effect_id != name
            or value.sequence != 0
            or value.recovery_ref is not None
            or value.state not in {"prepared", "applied"}
        ):
            raise WorkspaceError("workspace_recovery_required")
        native = TypeAdapter(Token).validate_python(value.native_ref, strict=True)
        if workspace_id is not None and native != workspace_id:
            raise WorkspaceError("workspace_recovery_required")
        return native

    def _complete(
        self,
        context: OperationContext,
        operation: LockedRecord,
        intent: OperationIntent,
        change: Change,
    ) -> Workspace:
        self._effect(context, intent.name, change.workspace.workspace_id, "applied")
        operation.save(intent.model_copy(update={"phase": "applied", "result": change.workspace}))
        self.store.finalize_operation(context, change.receipt)
        return change.workspace

    @staticmethod
    def _change(
        value: WorkspaceState, context: OperationContext, intent: OperationIntent
    ) -> Change | None:
        found = next((v for v in value.changes if v.operation_id == context.operation_id), None)
        if found is not None and found.request_digest != intent.request_digest:
            raise WorkspaceError("workspace_denied")
        return found
