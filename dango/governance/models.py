"""dango/governance/models.py

Pydantic V2 response models for data governance endpoints.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DriftEvent(BaseModel):
    """A single schema drift event."""

    model_config = ConfigDict(frozen=True)

    id: int
    source: str
    table_name: str
    column_name: str | None
    event_type: str
    detail: str | None
    detected_at: str


class DriftResponse(BaseModel):
    """Response model for the schema drift endpoint."""

    model_config = ConfigDict(frozen=True)

    events: list[DriftEvent]
    count: int
    source: str | None
    table_name: str | None


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
