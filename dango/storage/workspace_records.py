"""dango/storage/workspace_records.py

Define strict workspace records and immutable private recovery snapshots.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from dango.operations.contracts import OutcomeReceipt
from dango.storage.catalogs import CatalogBinding


def text(value: str) -> str:
    """Reject blank/control text without exposing rejected input."""
    if not value.strip() or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value):
        raise ValueError("Supply nonblank text without control characters.")
    return value


def timestamp(value: str) -> str:
    """Require an ISO timestamp with an explicit UTC offset."""
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)", value):
        raise ValueError("Supply an aware UTC ISO timestamp.")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        raise ValueError("Supply an aware UTC ISO timestamp.")
    return value


Text = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(text)]
Token = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Stamp = Annotated[str, AfterValidator(timestamp)]
Counter = Annotated[int, Field(ge=0)]
Purpose = Literal["input", "build", "query", "backup"]


class Record(BaseModel):
    """Reject coercion and unknown fields, including nested private snapshots."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)


class Workspace(Record):
    """Reference one actor's workspace; branch metadata conveys no authority."""

    workspace_id: Token
    project_id: Text
    actor_id: Text
    branch_ref: Text
    code_commit: Text
    execution_location: Literal["remote", "local"]
    catalog_binding_ref: Text
    data_root_ref: Annotated[str, AfterValidator(text)]
    created_at: Stamp
    lease_expiry: Stamp
    state: Literal[
        "allocating",
        "ready",
        "building",
        "checked",
        "failed",
        "previewable",
        "expired",
        "recovery_required",
    ]
    revision: Counter


class WorkspacePin(Record):
    """Bind a generated retention reference to its server-owned operation."""

    pin_id: Token
    operation_id: Text
    purpose: Purpose
    resource_ref: Text


class PinState(Record):
    """Retain released pins so retries cannot resurrect or replace their identity."""

    pin: WorkspacePin
    released: bool


class Change(Record):
    """Atomically retain a mutation result alongside its new workspace revision."""

    operation_id: Text
    request_digest: Text
    workspace: Workspace
    receipt: OutcomeReceipt


class WorkspaceState(Record):
    """Store references, never a second authoritative operation lifecycle."""

    kind: Literal["workspace-v1"] = "workspace-v1"
    workspace_id: Token
    project_id: Text
    actor_id: Text
    allocation_operation_id: Text
    phase: Literal["allocating", "ready", "removing", "expired"]
    workspace: Workspace | None
    binding: CatalogBinding | None
    operations: tuple[Text, ...]
    pins: tuple[PinState, ...]
    changes: tuple[Change, ...]
    removal_operation_id: Text | None = None
    removal_revision: Counter | None = None


class OperationIntent(Record):
    """Freeze domain arguments before preparing or resuming external effects."""

    kind: Literal["workspace-operation-v1"] = "workspace-operation-v1"
    operation_id: Text
    project_id: Text
    actor_id: Text
    name: Text
    request_digest: Text
    parameters_json: str
    phase: Literal["intent", "bound", "prepared", "applied"]
    workspace_id: Token | None
    result: Workspace | None = None


class WorkspaceError(RuntimeError):
    """Expose fixed safe codes/actions rather than private/native diagnostics."""

    def __init__(self, code: str):
        self.code = code
        self.action = {
            "invalid_input": "Supply valid workspace arguments and an aware UTC clock.",
            "workspace_denied": "Use the admitted server operation for the owning actor and project.",
            "workspace_conflict": "Read the current workspace revision before a new operation.",
            "workspace_recovery_required": "Retry the original operation or inspect retained ownership evidence.",
            "workspace_unavailable": "Inspect the private workspace state before retrying.",
        }[code]
        super().__init__(f"{code}: {self.action}")
