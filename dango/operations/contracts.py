"""dango/operations/contracts.py

Validate shared operation envelopes and compute local deterministic request identities.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import Self

__all__ = [
    "ContractValidationError",
    "OperationContext",
    "OperationRequest",
    "OutcomeReceipt",
    "canonical_request_bytes",
    "request_digest",
]


class ContractValidationError(ValueError):
    """Report invalid JSON payloads using only fixed, safe public error text."""

    def __init__(self) -> None:
        self.code = "invalid_payload"
        self.action = "Supply a JSON payload with string keys and finite numbers."
        super().__init__(f"{self.code}: {self.action}")


def _validate_text(value: str) -> str:
    if not value.strip() or any(unicodedata.category(char) == "Cc" for char in value):
        raise ValueError("Text must be nonblank and contain no control characters.")
    return value


_Identifier = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_validate_text)]
_ErrorText = Annotated[str, Field(min_length=1, max_length=512), AfterValidator(_validate_text)]


def _validate_json_value(value: object, ancestors: set[int]) -> None:
    value_type = type(value)
    if value is None or value_type in (bool, str, int):
        return
    if isinstance(value, float):
        if value_type is not float or not math.isfinite(value):
            raise ContractValidationError()
        return
    if value_type not in (list, dict):
        raise ContractValidationError()
    identity = id(value)
    if identity in ancestors:
        raise ContractValidationError()
    ancestors.add(identity)
    try:
        if isinstance(value, dict):
            for key, child in value.items():
                if type(key) is not str:
                    raise ContractValidationError()
                _validate_json_value(child, ancestors)
        elif isinstance(value, list):
            for child in value:
                _validate_json_value(child, ancestors)
    finally:
        ancestors.remove(identity)


def _validate_payload(value: object) -> object:
    if type(value) is not dict:
        raise ContractValidationError()
    try:
        _validate_json_value(value, set())
    except RecursionError:
        raise ContractValidationError() from None
    return value


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def _validate_schema_version(cls, value: object) -> object:
        # Literal[1] alone also accepts True because True == 1 in Python.
        if type(value) is not int or value != 1:
            raise ValueError("Schema version must be the integer 1.")
        return value


class OperationContext(_ContractModel):
    """Carry already-authenticated server context; construction grants no access."""

    operation_id: _Identifier
    project_id: _Identifier
    actor_id: _Identifier
    capability: _Identifier
    permission_revision: _Identifier
    trace_id: _Identifier


class OperationRequest(_ContractModel):
    """Carry operation input whose domain validation and authorization belong to its handler."""

    operation_name: _Identifier
    payload: dict[str, object]
    expected_revision: _Identifier | None = None
    idempotency_key: _Identifier | None = None

    @field_validator("payload", mode="before")
    @classmethod
    def _validate_payload_field(cls, value: object) -> object:
        return _validate_payload(value)


class OutcomeReceipt(_ContractModel):
    """Describe a final outcome with actionable errors for every non-success status."""

    operation_id: _Identifier
    status: Literal["success", "blocked", "failed", "cancelled", "recovery_required"]
    resource_revision: _Identifier | None = None
    result_ref: _Identifier | None = None
    evidence_refs: tuple[_Identifier, ...] = ()
    error_code: _ErrorText | None = None
    action: _ErrorText | None = None

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        if self.status == "success":
            if self.error_code is not None or self.action is not None:
                raise ValueError("Success receipts must not include error code or action.")
        elif self.error_code is None or self.action is None:
            raise ValueError("Non-success receipts require both error code and action.")
        return self


def _request_object(request: OperationRequest) -> dict[str, object]:
    # Frozen models are shallow: validate the current nested values on every call.
    _validate_payload(request.payload)
    return {
        "operation_name": request.operation_name,
        "payload": request.payload,
        "expected_revision": request.expected_revision,
    }


def _canonical_json_bytes(value: dict[str, object]) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ContractValidationError() from None


def canonical_request_bytes(request: OperationRequest) -> bytes:
    """Return deterministic UTF-8 JSON for this Python request, without logging or persistence."""
    return _canonical_json_bytes(_request_object(request))


def request_digest(context: OperationContext, request: OperationRequest) -> str:
    """Hash server identity and current request input, excluding retry and trace identifiers."""
    envelope: dict[str, object] = {
        "schema_version": context.schema_version,
        "project_id": context.project_id,
        "actor_id": context.actor_id,
        "capability": context.capability,
        "permission_revision": context.permission_revision,
        "request": _request_object(request),
    }
    return hashlib.sha256(_canonical_json_bytes(envelope)).hexdigest()
