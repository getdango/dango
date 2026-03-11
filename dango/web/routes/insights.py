"""dango/web/routes/insights.py

Automated insights API endpoints.

Provides read access to cached analysis results, on-demand analysis execution,
and metric history queries.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.logging import get_logger
from dango.validation import validate_identifier
from dango.web.helpers import get_project_root

logger = get_logger(__name__)

router = APIRouter(tags=["insights"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class InsightMetric(BaseModel):
    """A single metric in the insights response."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: str
    value: float | None = None
    change_pct: float | None = None
    comparison_type: str | None = None
    baseline_value: float | None = None
    exceeds_threshold: bool = False
    trend_direction: str | None = None
    forecast_threshold_days: int | None = None
    source: str | None = None
    table_name: str | None = None
    drill_down: list[dict] = []  # type: ignore[type-arg]
    error: str | None = None


class InsightsResponse(BaseModel):
    """Response for insights endpoints."""

    model_config = ConfigDict(frozen=True)

    metrics: list[InsightMetric]
    total: int
    flagged: int


class HistoryPoint(BaseModel):
    """A single data point in metric history."""

    model_config = ConfigDict(frozen=True)

    value: float | None = None
    recorded_at: str


class MetricHistoryResponse(BaseModel):
    """Response for metric history endpoint."""

    model_config = ConfigDict(frozen=True)

    metric: str
    days: int
    data_points: list[HistoryPoint]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_insights_response(categorized: list[dict]) -> InsightsResponse:  # type: ignore[type-arg]
    """Build an ``InsightsResponse`` from categorised result dicts.

    Args:
        categorized: Output of ``categorize_results()``.

    Returns:
        An ``InsightsResponse`` with metric list, total, and flagged count.
    """
    metrics = [InsightMetric(**m) for m in categorized]
    flagged_count = sum(1 for m in metrics if m.status == "flagged")
    return InsightsResponse(metrics=metrics, total=len(metrics), flagged=flagged_count)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/insights")
async def get_insights(
    user: User = Depends(require_permission("governance.view")),
) -> InsightsResponse:
    """Read latest cached analysis results.

    Returns categorised metrics from the most recent analysis run.
    """
    log_auth_event(AuditEvent.INSIGHTS_VIEWED, user_id=user.id, email=user.email)

    project_root = get_project_root()

    from dango.analysis.formatter import categorize_results
    from dango.analysis.metrics import run_analysis

    results = await asyncio.to_thread(run_analysis, project_root)
    categorized = categorize_results(results)
    return _build_insights_response(categorized)


@router.post("/api/insights/run")
async def run_insights(
    source: str | None = Query(None, description="Filter by source name"),
    user: User = Depends(require_permission("governance.view")),
) -> InsightsResponse:
    """Execute a fresh analysis run (state-mutating).

    Optionally filter by source name.
    """
    log_auth_event(AuditEvent.INSIGHTS_VIEWED, user_id=user.id, email=user.email)

    project_root = get_project_root()

    source_filter: list[str] | None = None
    if source is not None:
        source_filter = [f"raw_{source}"]

    from dango.analysis.formatter import categorize_results
    from dango.analysis.metrics import run_analysis

    results = await asyncio.to_thread(run_analysis, project_root, source_filter=source_filter)
    categorized = categorize_results(results)
    return _build_insights_response(categorized)


@router.get("/api/insights/history")
async def get_metric_history(
    metric: str = Query(..., description="Metric name"),
    days: int = Query(30, ge=1, le=365, description="Days of history"),
    user: User = Depends(require_permission("governance.view")),
) -> MetricHistoryResponse:
    """Read metric value history for a given metric.

    Returns up to ``days`` worth of historical data points.
    """
    log_auth_event(AuditEvent.INSIGHTS_VIEWED, user_id=user.id, email=user.email)

    validated_metric = validate_identifier(metric)
    project_root = get_project_root()

    from dango.utils.dango_db import connect

    def _query_history() -> list[HistoryPoint]:
        with connect(project_root) as conn:
            rows = conn.execute(
                "SELECT metric_value, recorded_at FROM metric_history "
                "WHERE metric_name = ? "
                "AND recorded_at >= datetime('now', '-' || ? || ' days') "
                "ORDER BY recorded_at DESC",
                (validated_metric, days),
            ).fetchall()
        return [HistoryPoint(value=row[0], recorded_at=row[1]) for row in rows]

    data_points = await asyncio.to_thread(_query_history)
    return MetricHistoryResponse(metric=validated_metric, days=days, data_points=data_points)
