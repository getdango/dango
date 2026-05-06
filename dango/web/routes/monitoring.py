"""dango/web/routes/monitoring.py

Automated monitoring API endpoints.

Provides read access to cached analysis results, on-demand analysis execution,
and metric history queries.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from starlette.responses import RedirectResponse

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.logging import get_logger
from dango.validation import validate_identifier
from dango.web.helpers import get_project_root

logger = get_logger(__name__)

router = APIRouter(tags=["monitoring"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class MonitorMetric(BaseModel):
    """A single metric in the monitoring response."""

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


# Backward-compatible alias
InsightMetric = MonitorMetric


class DbtTestResult(BaseModel):
    """A single dbt test result."""

    model_config = ConfigDict(frozen=True)

    test_name: str
    status: str | None = None  # "pass" | "fail" | "error" | None
    model_name: str | None = None
    execution_time: float | None = None


class MonitoringResponse(BaseModel):
    """Response for monitoring endpoints."""

    model_config = ConfigDict(frozen=True)

    metrics: list[MonitorMetric]
    total: int
    flagged: int
    dbt_tests: list[DbtTestResult] = []


# Backward-compatible alias
InsightsResponse = MonitoringResponse


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


def _read_dbt_test_results(project_root: Path) -> list[DbtTestResult]:
    """Parse dbt test results from ``run_results.json`` + ``manifest.json``.

    Extracts test nodes from the manifest and maps each to its execution
    status from the most recent ``run_results.json``.

    Args:
        project_root: Path to the Dango project root.

    Returns:
        List of :class:`DbtTestResult` entries.  Empty if files are missing.
    """
    manifest_path = project_root / "dbt" / "target" / "manifest.json"
    results_path = project_root / "dbt" / "target" / "run_results.json"

    if not manifest_path.exists():
        return []

    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception:
        return []

    # Build status + timing lookup from run_results
    result_status: dict[str, str] = {}
    result_time: dict[str, float] = {}
    if results_path.exists():
        try:
            run_results = json.loads(results_path.read_text())
            for r in run_results.get("results", []):
                uid = r.get("unique_id", "")
                if uid:
                    result_status[uid] = r.get("status", "")
                    result_time[uid] = r.get("execution_time", 0.0)
        except Exception:
            pass

    tests: list[DbtTestResult] = []
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "test":
            continue
        test_name = node.get("name", uid)
        status = result_status.get(uid)
        exec_time = result_time.get(uid)

        # Derive model name from test dependencies
        model_name: str | None = None
        for dep in node.get("depends_on", {}).get("nodes", []):
            if dep.startswith("model."):
                parts = dep.split(".")
                model_name = parts[-1] if len(parts) >= 2 else dep
                break

        tests.append(
            DbtTestResult(
                test_name=test_name,
                status=status,
                model_name=model_name,
                execution_time=exec_time,
            )
        )

    return tests


def _build_monitoring_response(
    categorized: list[dict],  # type: ignore[type-arg]
    dbt_tests: list[DbtTestResult] | None = None,
) -> MonitoringResponse:
    """Build a ``MonitoringResponse`` from categorised result dicts.

    Args:
        categorized: Output of ``categorize_results()``.
        dbt_tests: Optional list of dbt test results.

    Returns:
        A ``MonitoringResponse`` with metric list, total, and flagged count.
    """
    metrics = [MonitorMetric(**m) for m in categorized]
    flagged_count = sum(1 for m in metrics if m.status == "flagged")
    return MonitoringResponse(
        metrics=metrics,
        total=len(metrics),
        flagged=flagged_count,
        dbt_tests=dbt_tests or [],
    )


def _read_cached_results(project_root: Path) -> list[dict[str, Any]]:
    """Read the most recent analysis results from SQLite cache.

    Queries ``metric_history`` for latest values and ``metric_results`` for
    latest comparison data per metric.  Returns categorised dicts ready for
    ``_build_monitoring_response()``.

    Args:
        project_root: Path to the Dango project root.

    Returns:
        List of categorised metric dicts (same shape as
        ``categorize_results()`` output).
    """
    from dango.utils.dango_db import connect

    with connect(project_root) as conn:
        # Latest value per metric from metric_history
        history_rows = conn.execute(
            "SELECT h.metric_name, h.metric_value, h.source, h.table_name "
            "FROM metric_history h "
            "INNER JOIN ("
            "  SELECT metric_name, MAX(recorded_at) AS max_at "
            "  FROM metric_history GROUP BY metric_name"
            ") latest ON h.metric_name = latest.metric_name "
            "AND h.recorded_at = latest.max_at"
        ).fetchall()

        # Latest comparison per metric from metric_results
        result_rows = conn.execute(
            "SELECT r.metric_name, r.result_value "
            "FROM metric_results r "
            "INNER JOIN ("
            "  SELECT metric_name, MAX(computed_at) AS max_at "
            "  FROM metric_results GROUP BY metric_name"
            ") latest ON r.metric_name = latest.metric_name "
            "AND r.computed_at = latest.max_at"
        ).fetchall()

    # Index comparison results by metric_name
    comparisons: dict[str, dict[str, Any]] = {}
    for metric_name, result_value in result_rows:
        if result_value:
            try:
                comparisons[metric_name] = json.loads(result_value)
            except (json.JSONDecodeError, TypeError):
                pass

    categorized: list[dict[str, Any]] = []
    for metric_name, value, source, table_name in history_rows:
        comp = comparisons.get(metric_name, {})
        exceeds = comp.get("exceeds_threshold", False)
        trend_dir = comp.get("trend_direction")

        # Assign status
        if exceeds:
            status = "flagged"
        elif trend_dir is not None and trend_dir != "stable":
            status = "trending"
        else:
            status = "normal"

        categorized.append(
            {
                "name": metric_name,
                "status": status,
                "value": value,
                "change_pct": comp.get("change_pct"),
                "comparison_type": comp.get("comparison_type"),
                "baseline_value": comp.get("baseline_value"),
                "exceeds_threshold": exceeds,
                "trend_direction": trend_dir,
                "forecast_threshold_days": comp.get("forecast_threshold_days"),
                "source": source,
                "table_name": table_name,
                "drill_down": [],
                "error": None,
            }
        )

    # Sort: flagged first, then trending, normal
    status_order = {"flagged": 0, "trending": 1, "normal": 2, "error": 3}
    categorized.sort(
        key=lambda d: (
            status_order.get(d["status"], 4),
            -(abs(d["change_pct"]) if d["change_pct"] is not None else 0),
        )
    )
    return categorized


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/monitoring")
async def get_monitoring(
    user: User = Depends(require_permission("governance.view")),
) -> MonitoringResponse:
    """Read latest cached analysis results.

    Returns categorised metrics from the most recent analysis run.
    Does not execute new analysis — use ``POST /api/monitoring/run`` for that.
    """
    log_auth_event(AuditEvent.MONITORING_VIEWED, user_id=user.id, email=user.email)

    project_root = get_project_root()
    categorized = await asyncio.to_thread(_read_cached_results, project_root)
    dbt_tests = await asyncio.to_thread(_read_dbt_test_results, project_root)
    return _build_monitoring_response(categorized, dbt_tests=dbt_tests)


@router.post("/api/monitoring/run")
async def run_monitoring(
    source: str | None = Query(None, description="Filter by source name"),
    user: User = Depends(require_permission("governance.view")),
) -> MonitoringResponse:
    """Execute a fresh analysis run (state-mutating).

    Optionally filter by source name.
    """
    log_auth_event(AuditEvent.MONITORING_VIEWED, user_id=user.id, email=user.email)

    project_root = get_project_root()

    source_filter: list[str] | None = None
    if source is not None:
        validated = validate_identifier(source)
        source_filter = [f"raw_{validated}"]

    from dango.analysis.formatter import categorize_results
    from dango.analysis.metrics import run_analysis

    results = await asyncio.to_thread(run_analysis, project_root, source_filter=source_filter)
    categorized = categorize_results(results)
    dbt_tests = await asyncio.to_thread(_read_dbt_test_results, project_root)
    return _build_monitoring_response(categorized, dbt_tests=dbt_tests)


@router.get("/api/monitoring/history")
async def get_metric_history(
    metric: str = Query(..., description="Metric name"),
    days: int = Query(30, ge=1, le=365, description="Days of history"),
    user: User = Depends(require_permission("governance.view")),
) -> MetricHistoryResponse:
    """Read metric value history for a given metric.

    Returns up to ``days`` worth of historical data points.
    """
    log_auth_event(AuditEvent.MONITORING_VIEWED, user_id=user.id, email=user.email)

    validated_metric = validate_identifier(metric)
    project_root = get_project_root()

    from dango.utils.dango_db import connect

    def _query_history() -> list[HistoryPoint]:
        with connect(project_root) as conn:
            rows = conn.execute(
                "SELECT metric_value, recorded_at FROM metric_history "
                "WHERE metric_name = ? "
                "AND recorded_at >= datetime('now', '-' || ? || ' days') "
                "ORDER BY recorded_at DESC "
                "LIMIT 1000",
                (validated_metric, days),
            ).fetchall()
        return [HistoryPoint(value=row[0], recorded_at=row[1]) for row in rows]

    data_points = await asyncio.to_thread(_query_history)
    return MetricHistoryResponse(metric=validated_metric, days=days, data_points=data_points)


# ---------------------------------------------------------------------------
# Backward-compatible redirects for old /api/insights* paths
# ---------------------------------------------------------------------------


@router.get("/api/insights")
async def redirect_insights_get(
    user: User = Depends(require_permission("governance.view")),
) -> RedirectResponse:
    """Redirect old GET /api/insights to /api/monitoring."""
    return RedirectResponse(url="/api/monitoring", status_code=301)


@router.post("/api/insights/run")
async def redirect_insights_run(
    user: User = Depends(require_permission("governance.view")),
) -> RedirectResponse:
    """Redirect old POST /api/insights/run to /api/monitoring/run."""
    return RedirectResponse(url="/api/monitoring/run", status_code=307)


@router.get("/api/insights/history")
async def redirect_insights_history(
    user: User = Depends(require_permission("governance.view")),
) -> RedirectResponse:
    """Redirect old GET /api/insights/history to /api/monitoring/history."""
    return RedirectResponse(url="/api/monitoring/history", status_code=301)
