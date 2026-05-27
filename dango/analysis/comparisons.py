"""dango/analysis/comparisons.py

Comparison engine for metric values against historical baselines.

Queries the ``metric_history`` table in ``.dango/dango.db`` (SQLite) to
compute week-over-week, rolling average, and prior-period comparisons.
Also provides linear-regression trend detection over the last 30 data points.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from dango.analysis.models import ComparisonResult, ComparisonType, MetricValue
from dango.logging import get_logger
from dango.utils.dango_db import connect

logger = get_logger(__name__)

MIN_TREND_POINTS = 14
MAX_TREND_POINTS = 30
TREND_THRESHOLD_PCT = 0.01  # 1% of mean
MAX_FORECAST_DAYS = 365


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_comparison(
    project_root: Path,
    metric_value: MetricValue,
    comparison_type: ComparisonType,
    alert_threshold: float | None,
) -> ComparisonResult:
    """Compare a metric value against its historical baseline.

    Args:
        project_root: Path to the Dango project root.
        metric_value: The current metric value.
        comparison_type: Which comparison strategy to use.
        alert_threshold: Percentage change threshold for alerts.

    Returns:
        A ``ComparisonResult`` with computed change percentage and threshold flag.
    """
    current = metric_value.value
    if current is None:
        return ComparisonResult(
            metric_name=metric_value.metric_name,
            comparison_type=comparison_type,
        )

    with connect(project_root) as conn:
        baseline = _get_baseline(conn, metric_value.metric_name, comparison_type)

    change_pct = _compute_change_pct(current, baseline)
    exceeds = (
        alert_threshold is not None
        and change_pct is not None
        and abs(change_pct) >= alert_threshold
    )

    slope, direction, forecast_days = detect_trend(
        project_root, metric_value.metric_name, alert_threshold
    )

    return ComparisonResult(
        metric_name=metric_value.metric_name,
        comparison_type=comparison_type,
        current_value=current,
        baseline_value=baseline,
        change_pct=change_pct,
        exceeds_threshold=exceeds,
        trend_slope=slope,
        trend_direction=direction,
        forecast_threshold_days=forecast_days,
    )


def detect_trend(
    project_root: Path,
    metric_name: str,
    alert_threshold: float | None = None,
) -> tuple[float | None, str | None, int | None]:
    """Detect a linear trend over the last 30 data points.

    Requires at least ``MIN_TREND_POINTS`` (14) data points.

    Args:
        project_root: Path to the Dango project root.
        metric_name: Name of the metric to analyse.
        alert_threshold: Optional threshold percentage for forecasting.

    Returns:
        A tuple of ``(slope, direction, forecast_days)``.  All ``None`` if
        insufficient data.
    """
    with connect(project_root) as conn:
        rows = conn.execute(
            "SELECT metric_value FROM metric_history "
            "WHERE metric_name = ? AND metric_value IS NOT NULL "
            "ORDER BY recorded_at DESC LIMIT ?",
            (metric_name, MAX_TREND_POINTS),
        ).fetchall()

    if len(rows) < MIN_TREND_POINTS:
        return None, None, None

    # Oldest first for regression
    ys = [float(r["metric_value"]) for r in reversed(rows)]
    xs = list(range(len(ys)))

    slope, _intercept = _linear_regression(xs, ys)

    mean_y = sum(ys) / len(ys) if ys else 0.0
    threshold_abs = abs(mean_y * TREND_THRESHOLD_PCT) if mean_y != 0 else 0.0

    if slope > threshold_abs:
        direction = "increasing"
    elif slope < -threshold_abs:
        direction = "decreasing"
    else:
        direction = "stable"

    forecast_days = _forecast_threshold_days(ys[-1], slope, alert_threshold, mean_y)

    return slope, direction, forecast_days


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_baseline(
    conn: sqlite3.Connection,
    metric_name: str,
    comparison_type: ComparisonType,
) -> float | None:
    """Dispatch to the appropriate baseline computation."""
    if comparison_type == ComparisonType.week_over_week:
        return _compare_week_over_week(conn, metric_name)
    if comparison_type == ComparisonType.rolling_7day_avg:
        return _compare_rolling_avg(conn, metric_name, 7)
    if comparison_type == ComparisonType.rolling_30day_avg:
        return _compare_rolling_avg(conn, metric_name, 30)
    if comparison_type == ComparisonType.prior_period:
        return _compare_prior_period(conn, metric_name)
    return None  # pragma: no cover


def _compare_week_over_week(
    conn: sqlite3.Connection,
    metric_name: str,
) -> float | None:
    """Return the value closest to 7 days ago."""
    row = conn.execute(
        "SELECT metric_value FROM metric_history "
        "WHERE metric_name = ? AND metric_value IS NOT NULL "
        "ORDER BY ABS(julianday(recorded_at) - julianday('now', '-7 days')) "
        "LIMIT 1",
        (metric_name,),
    ).fetchone()
    return float(row["metric_value"]) if row else None


def _compare_rolling_avg(
    conn: sqlite3.Connection,
    metric_name: str,
    days: int,
) -> float | None:
    """Return the average metric value over the last *days* days."""
    row = conn.execute(
        "SELECT AVG(metric_value) AS avg_val FROM metric_history "
        "WHERE metric_name = ? AND metric_value IS NOT NULL "
        "AND recorded_at >= datetime('now', ?)",
        (metric_name, f"-{days} days"),
    ).fetchone()
    if row and row["avg_val"] is not None:
        return float(row["avg_val"])
    return None


def _compare_prior_period(
    conn: sqlite3.Connection,
    metric_name: str,
) -> float | None:
    """Return the most recent previous value (skipping the latest)."""
    row = conn.execute(
        "SELECT metric_value FROM metric_history "
        "WHERE metric_name = ? AND metric_value IS NOT NULL "
        "ORDER BY recorded_at DESC LIMIT 1 OFFSET 1",
        (metric_name,),
    ).fetchone()
    return float(row["metric_value"]) if row else None


def _compute_change_pct(
    current: float,
    baseline: float | None,
) -> float | None:
    """Compute percentage change from baseline to current.

    Returns ``None`` when baseline is ``None`` or zero (avoid division by zero).
    """
    if baseline is None or baseline == 0:
        return None
    return ((current - baseline) / abs(baseline)) * 100


def _linear_regression(
    xs: Sequence[int | float],
    ys: Sequence[float],
) -> tuple[float, float]:
    """Ordinary least squares for a simple linear model.

    Args:
        xs: Independent variable values.
        ys: Dependent variable values.

    Returns:
        ``(slope, intercept)`` tuple.
    """
    n = len(xs)
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys, strict=True))
    sum_x2 = sum(x * x for x in xs)

    denominator = n * sum_x2 - sum_x * sum_x
    if denominator == 0:
        return 0.0, (sum_y / n if n else 0.0)

    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def _forecast_threshold_days(
    current_value: float,
    slope: float,
    alert_threshold: float | None,
    mean_value: float,
) -> int | None:
    """Estimate days until the alert_threshold percentage change is exceeded.

    Returns ``None`` when forecasting is not applicable (no threshold,
    zero slope, or already exceeded).
    """
    if alert_threshold is None or slope == 0 or mean_value == 0:
        return None

    # Threshold in absolute terms relative to mean
    threshold_abs = abs(mean_value * alert_threshold / 100)
    distance = threshold_abs - abs(current_value - mean_value)

    if distance <= 0:
        # Already exceeded
        return 0

    days = int(distance / abs(slope))
    return min(days, MAX_FORECAST_DAYS) if days > 0 else None
