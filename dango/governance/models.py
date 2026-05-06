"""dango/governance/models.py

Pydantic V2 response models for data governance endpoints.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class DriftEvent(BaseModel):
    """A single schema drift event."""

    model_config = ConfigDict(frozen=True)

    id: int
    source: str
    table_name: str
    column_name: str | None
    event_type: str
    severity: str | None = None
    detail: str | None
    detected_at: str


class DriftResponse(BaseModel):
    """Response model for the schema drift endpoint."""

    model_config = ConfigDict(frozen=True)

    events: list[DriftEvent]
    count: int
    source: str | None
    table_name: str | None


class SourceAttention(BaseModel):
    """A source that needs user attention due to breaking drift."""

    model_config = ConfigDict(frozen=True)

    source: str
    reason: str
    drift_events: list[dict[str, Any]] = []
    created_at: str


class AcceptDriftResponse(BaseModel):
    """Response from accepting schema drift for a source."""

    model_config = ConfigDict(frozen=True)

    source: str
    accepted: bool
    message: str


class PiiFinding(BaseModel):
    """A single PII finding from column scanning."""

    model_config = ConfigDict(frozen=True)

    id: int
    source: str
    table_name: str
    column_name: str
    entity_type: str
    confidence: float | None
    sample_count: int | None
    scanned_at: str


class PiiResponse(BaseModel):
    """Response model for the PII findings endpoint."""

    model_config = ConfigDict(frozen=True)

    findings: list[PiiFinding]
    count: int
    source: str | None
    table_name: str | None


class PiiOverride(BaseModel):
    """A single PII override record."""

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    source: str
    table_name: str
    column_name: str
    pii_status: str
    set_by: str
    reason: str | None
    updated_at: str


class PiiOverridesResponse(BaseModel):
    """Response model for listing PII overrides."""

    model_config = ConfigDict(frozen=True)

    overrides: list[PiiOverride]
    count: int
