"""tests/unit/test_analysis_comparisons.py

Tests for the comparison engine (dango/analysis/comparisons.py).

Uses real SQLite via ``dango.utils.dango_db.connect()`` with pre-populated
``metric_history`` data in ``tmp_path``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dango.analysis.comparisons import (
    _compute_change_pct,
    _linear_regression,
    compute_comparison,
    detect_trend,
)
from dango.analysis.models import ComparisonType, MetricValue
from dango.utils.dango_db import connect


def _insert_history(tmp_path, metric_name, value, days_ago=0):
    """Insert a metric_history row at a given offset from now."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with connect(tmp_path) as conn:
        conn.execute(
            "INSERT INTO metric_history (metric_name, metric_value, recorded_at) VALUES (?, ?, ?)",
            (metric_name, value, ts),
        )
        conn.commit()


@pytest.mark.unit
class TestComputeChangePct:
    """_compute_change_pct edge cases."""

    def test_normal(self):
        """Normal percentage change."""
        assert _compute_change_pct(110, 100) == pytest.approx(10.0)

    def test_negative_change(self):
        """Decrease from baseline."""
        assert _compute_change_pct(90, 100) == pytest.approx(-10.0)

    def test_baseline_none(self):
        """None baseline returns None."""
        assert _compute_change_pct(100, None) is None

    def test_baseline_zero(self):
        """Zero baseline returns None (avoid division by zero)."""
        assert _compute_change_pct(100, 0) is None

    def test_negative_baseline(self):
        """Negative baseline uses absolute value for denominator."""
        result = _compute_change_pct(-80, -100)
        assert result == pytest.approx(20.0)


@pytest.mark.unit
class TestLinearRegression:
    """_linear_regression OLS formula."""

    def test_perfect_positive(self):
        """Perfect linear increase."""
        slope, intercept = _linear_regression([0, 1, 2, 3], [0, 1, 2, 3])
        assert slope == pytest.approx(1.0)
        assert intercept == pytest.approx(0.0)

    def test_flat(self):
        """Constant values → slope 0."""
        slope, _intercept = _linear_regression([0, 1, 2, 3], [5, 5, 5, 5])
        assert slope == pytest.approx(0.0)

    def test_negative_slope(self):
        """Decreasing values."""
        slope, _intercept = _linear_regression([0, 1, 2], [10, 5, 0])
        assert slope == pytest.approx(-5.0)

    def test_single_point(self):
        """Single point returns zero slope."""
        slope, intercept = _linear_regression([0], [42])
        assert slope == pytest.approx(0.0)
        assert intercept == pytest.approx(42.0)


@pytest.mark.unit
class TestComputeComparison:
    """compute_comparison integration with SQLite history."""

    def test_week_over_week(self, tmp_path):
        """Week-over-week comparison finds value from ~7 days ago."""
        _insert_history(tmp_path, "revenue", 100.0, days_ago=7)
        _insert_history(tmp_path, "revenue", 110.0, days_ago=0)

        mv = MetricValue(metric_name="revenue", value=110.0)
        result = compute_comparison(
            tmp_path, mv, ComparisonType.week_over_week, alert_threshold=15.0
        )
        assert result.baseline_value == pytest.approx(100.0)
        assert result.change_pct == pytest.approx(10.0)
        assert result.exceeds_threshold is False

    def test_exceeds_threshold(self, tmp_path):
        """Threshold exceeded when change_pct >= warn_threshold."""
        _insert_history(tmp_path, "revenue", 100.0, days_ago=7)
        _insert_history(tmp_path, "revenue", 120.0, days_ago=0)

        mv = MetricValue(metric_name="revenue", value=120.0)
        result = compute_comparison(
            tmp_path, mv, ComparisonType.week_over_week, alert_threshold=10.0
        )
        assert result.exceeds_threshold is True

    def test_rolling_7day_avg(self, tmp_path):
        """Rolling 7-day average comparison."""
        for i in range(7):
            _insert_history(tmp_path, "users", 100.0 + i, days_ago=i)

        mv = MetricValue(metric_name="users", value=110.0)
        result = compute_comparison(
            tmp_path, mv, ComparisonType.rolling_7day_avg, alert_threshold=None
        )
        assert result.baseline_value is not None
        assert result.baseline_value == pytest.approx(103.0)

    def test_rolling_30day_avg(self, tmp_path):
        """Rolling 30-day average comparison."""
        for i in range(10):
            _insert_history(tmp_path, "orders", 50.0, days_ago=i)

        mv = MetricValue(metric_name="orders", value=60.0)
        result = compute_comparison(
            tmp_path, mv, ComparisonType.rolling_30day_avg, alert_threshold=None
        )
        assert result.baseline_value == pytest.approx(50.0)

    def test_prior_period(self, tmp_path):
        """Prior period comparison uses second-most-recent value."""
        _insert_history(tmp_path, "signups", 80.0, days_ago=1)
        _insert_history(tmp_path, "signups", 90.0, days_ago=0)

        mv = MetricValue(metric_name="signups", value=90.0)
        result = compute_comparison(tmp_path, mv, ComparisonType.prior_period, alert_threshold=None)
        assert result.baseline_value == pytest.approx(80.0)

    def test_no_history_returns_none_baseline(self, tmp_path):
        """First run — no history returns None baseline and change_pct."""
        mv = MetricValue(metric_name="new_metric", value=100.0)
        result = compute_comparison(
            tmp_path, mv, ComparisonType.week_over_week, alert_threshold=10.0
        )
        assert result.baseline_value is None
        assert result.change_pct is None
        assert result.exceeds_threshold is False

    def test_none_value_returns_empty_result(self, tmp_path):
        """Metric with None value returns minimal ComparisonResult."""
        mv = MetricValue(metric_name="broken", value=None, error="query failed")
        result = compute_comparison(
            tmp_path, mv, ComparisonType.week_over_week, alert_threshold=10.0
        )
        assert result.current_value is None
        assert result.baseline_value is None


@pytest.mark.unit
class TestDetectTrend:
    """detect_trend linear regression on metric_history."""

    def test_insufficient_data(self, tmp_path):
        """Fewer than 14 data points returns all None."""
        for i in range(10):
            _insert_history(tmp_path, "sparse", 100.0, days_ago=i)

        slope, direction, forecast = detect_trend(tmp_path, "sparse")
        assert slope is None
        assert direction is None
        assert forecast is None

    def test_increasing_trend(self, tmp_path):
        """Clear upward trend detected."""
        for i in range(20):
            _insert_history(tmp_path, "growing", float(100 + i * 10), days_ago=20 - i)

        slope, direction, _forecast = detect_trend(tmp_path, "growing")
        assert slope is not None
        assert slope > 0
        assert direction == "increasing"

    def test_decreasing_trend(self, tmp_path):
        """Clear downward trend detected."""
        for i in range(20):
            _insert_history(tmp_path, "shrinking", float(200 - i * 10), days_ago=20 - i)

        slope, direction, _forecast = detect_trend(tmp_path, "shrinking")
        assert slope is not None
        assert slope < 0
        assert direction == "decreasing"

    def test_stable_trend(self, tmp_path):
        """Flat data detected as stable."""
        for i in range(20):
            _insert_history(tmp_path, "flat", 100.0, days_ago=20 - i)

        slope, direction, _forecast = detect_trend(tmp_path, "flat")
        assert slope is not None
        assert direction == "stable"

    def test_no_data(self, tmp_path):
        """No data at all returns all None."""
        slope, direction, forecast = detect_trend(tmp_path, "nonexistent")
        assert slope is None
        assert direction is None
        assert forecast is None
