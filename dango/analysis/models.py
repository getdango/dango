"""dango/analysis/models.py

Pydantic V2 models for the monitoring engine, comparison system, and drill-down.

Defines configuration shapes (``MonitorConfig``, ``MonitorsConfig``), runtime
value containers (``MetricValue``, ``ComparisonResult``), drill-down models
(``DimensionContributor``, ``DrillDownDimension``), and the top-level
``AnalysisResult`` that bundles a metric value with its comparison and
drill-down results.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ComparisonType(str, Enum):
    """Supported comparison strategies."""

    none = "none"
    week_over_week = "week_over_week"
    rolling_7day_avg = "rolling_7day_avg"
    rolling_30day_avg = "rolling_30day_avg"
    prior_period = "prior_period"


# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------


class MonitorConfig(BaseModel):
    """Single monitor definition from ``.dango/monitors.yml``."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    name: str
    source_table: str
    value_expression: str
    filter: str | None = None
    compare: ComparisonType = ComparisonType.week_over_week
    alert_threshold: float | None = Field(
        default=None,
        validation_alias=AliasChoices("alert_threshold", "warn_threshold"),
    )
    drill_down: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            msg = (
                f"Monitor name must be lowercase alphanumeric with underscores, "
                f"starting with a letter: {v!r}"
            )
            raise ValueError(msg)
        return v

    @field_validator("source_table")
    @classmethod
    def _source_table_has_dot(cls, v: str) -> str:
        if "." not in v:
            msg = f"source_table must be schema-qualified (schema.table): {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("value_expression")
    @classmethod
    def _value_expression_not_empty(cls, v: str) -> str:
        if not v.strip():
            msg = "value_expression must not be empty"
            raise ValueError(msg)
        return v

    @field_validator("alert_threshold")
    @classmethod
    def _alert_threshold_positive(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            msg = f"alert_threshold must be positive: {v}"
            raise ValueError(msg)
        return v


# Backward-compatible aliases
MetricConfig = MonitorConfig


class MonitorsConfig(BaseModel):
    """Top-level monitors configuration from ``.dango/monitors.yml``."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    monitors: list[MonitorConfig] = Field(
        default_factory=list,
        validation_alias=AliasChoices("monitors", "metrics"),
    )
    enabled: bool = True


# Backward-compatible alias
MetricsConfig = MonitorsConfig


# ---------------------------------------------------------------------------
# Runtime value models
# ---------------------------------------------------------------------------


class MetricValue(BaseModel):
    """Result of executing a single metric query against DuckDB."""

    model_config = ConfigDict(frozen=True)

    metric_name: str
    source: str | None = None
    table_name: str | None = None
    value: float | None = None
    error: str | None = None


class ComparisonResult(BaseModel):
    """Result of comparing a metric value against a historical baseline."""

    model_config = ConfigDict(frozen=True)

    metric_name: str
    comparison_type: ComparisonType
    current_value: float | None = None
    baseline_value: float | None = None
    change_pct: float | None = None
    exceeds_threshold: bool = False
    trend_slope: float | None = None
    trend_direction: str | None = None
    forecast_threshold_days: int | None = None


class DimensionContributor(BaseModel):
    """A single group's contribution to a metric change."""

    model_config = ConfigDict(frozen=True)

    group_value: str | None = None
    current_value: float = 0.0
    previous_value: float | None = None
    change_pct: float | None = None
    change_abs: float | None = None


class DrillDownDimension(BaseModel):
    """Drill-down results for a single dimension."""

    model_config = ConfigDict(frozen=True)

    dimension: str
    contributors: list[DimensionContributor] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    """Bundles a metric value with its optional comparison."""

    model_config = ConfigDict(frozen=True)

    metric: MetricValue
    comparison: ComparisonResult | None = None
    drill_down: list[DrillDownDimension] = Field(default_factory=list)
