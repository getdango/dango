"""tests/unit/test_analysis_formatter.py

Tests for dango/analysis/formatter.py — categorize_results and format_webhook_summary.
"""

from __future__ import annotations

import pytest

from dango.analysis.formatter import categorize_results, format_webhook_summary
from dango.analysis.models import (
    AnalysisResult,
    ComparisonResult,
    ComparisonType,
    DimensionContributor,
    DrillDownDimension,
    MetricValue,
)


def _make_result(
    name: str = "test_metric",
    value: float | None = 100.0,
    error: str | None = None,
    exceeds_threshold: bool = False,
    change_pct: float | None = None,
    trend_direction: str | None = None,
    source: str | None = "raw_stripe",
    table_name: str | None = "payments",
    drill_down: list[DrillDownDimension] | None = None,
) -> AnalysisResult:
    """Build an AnalysisResult for testing."""
    metric = MetricValue(
        metric_name=name,
        value=value,
        error=error,
        source=source,
        table_name=table_name,
    )
    comparison: ComparisonResult | None = None
    if error is None:
        comparison = ComparisonResult(
            metric_name=name,
            comparison_type=ComparisonType.week_over_week,
            current_value=value,
            baseline_value=80.0,
            change_pct=change_pct,
            exceeds_threshold=exceeds_threshold,
            trend_direction=trend_direction,
        )
    return AnalysisResult(
        metric=metric,
        comparison=comparison,
        drill_down=drill_down or [],
    )


# ---------------------------------------------------------------------------
# categorize_results
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCategorizeResults:
    """Tests for categorize_results()."""

    def test_empty_input(self) -> None:
        """Empty list returns empty list."""
        assert categorize_results([]) == []

    def test_status_assignment_normal(self) -> None:
        """Normal result gets status 'normal'."""
        results = categorize_results([_make_result(change_pct=5.0)])
        assert results[0]["status"] == "normal"

    def test_status_assignment_flagged(self) -> None:
        """Flagged result gets status 'flagged'."""
        results = categorize_results([_make_result(exceeds_threshold=True, change_pct=25.0)])
        assert results[0]["status"] == "flagged"

    def test_status_assignment_trending(self) -> None:
        """Trending result gets status 'trending'."""
        results = categorize_results([_make_result(trend_direction="increasing", change_pct=5.0)])
        assert results[0]["status"] == "trending"

    def test_status_assignment_error(self) -> None:
        """Error result gets status 'error'."""
        results = categorize_results([_make_result(error="SQL error", value=None)])
        assert results[0]["status"] == "error"

    def test_sort_order(self) -> None:
        """Results are sorted: flagged > trending > normal > error."""
        items = [
            _make_result(name="normal_m", change_pct=1.0),
            _make_result(name="error_m", error="fail", value=None),
            _make_result(name="flagged_m", exceeds_threshold=True, change_pct=50.0),
            _make_result(name="trending_m", trend_direction="decreasing", change_pct=3.0),
        ]
        categorized = categorize_results(items)
        statuses = [c["status"] for c in categorized]
        assert statuses == ["flagged", "trending", "normal", "error"]

    def test_sort_within_group_by_change_pct(self) -> None:
        """Within same status, sorted by abs(change_pct) descending."""
        items = [
            _make_result(name="small", change_pct=2.0),
            _make_result(name="big", change_pct=-10.0),
        ]
        categorized = categorize_results(items)
        names = [c["name"] for c in categorized]
        assert names == ["big", "small"]

    def test_dict_fields_present(self) -> None:
        """Output dicts have all expected fields."""
        results = categorize_results([_make_result(change_pct=5.0)])
        item = results[0]
        expected_keys = {
            "name",
            "status",
            "value",
            "change_pct",
            "comparison_type",
            "baseline_value",
            "exceeds_threshold",
            "trend_direction",
            "forecast_threshold_days",
            "source",
            "table_name",
            "drill_down",
            "error",
        }
        assert set(item.keys()) == expected_keys

    def test_drill_down_included(self) -> None:
        """Drill-down data is included in output."""
        dim = DrillDownDimension(
            dimension="region",
            contributors=[
                DimensionContributor(
                    group_value="US",
                    current_value=100.0,
                    previous_value=80.0,
                    change_pct=25.0,
                    change_abs=20.0,
                ),
            ],
        )
        results = categorize_results([_make_result(change_pct=5.0, drill_down=[dim])])
        assert len(results[0]["drill_down"]) == 1
        assert results[0]["drill_down"][0]["dimension"] == "region"
        assert results[0]["drill_down"][0]["contributors"][0]["group_value"] == "US"


# ---------------------------------------------------------------------------
# format_webhook_summary
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFormatWebhookSummary:
    """Tests for format_webhook_summary()."""

    def test_no_flagged(self) -> None:
        """No flagged results produces 'No issues' summary."""
        results = [_make_result(name="ok", change_pct=1.0)]
        summary = format_webhook_summary(results, flagged=[])
        assert "No issues" in summary
        assert "1 total" in summary

    def test_with_flagged(self) -> None:
        """Flagged results appear in summary with metric name and change."""
        flagged = [_make_result(name="revenue", exceeds_threshold=True, change_pct=25.3)]
        summary = format_webhook_summary(flagged + [_make_result()], flagged)
        assert "1 flagged" in summary
        assert "revenue" in summary
        assert "+25.3%" in summary
        assert "2 total" in summary

    def test_with_trending(self) -> None:
        """Trending count appears in summary."""
        trending = _make_result(name="growing", trend_direction="increasing", change_pct=3.0)
        summary = format_webhook_summary([trending], flagged=[])
        assert "1 trending" in summary

    def test_caps_flagged_details(self) -> None:
        """Only first 3 flagged details are shown."""
        flagged = [
            _make_result(name=f"m{i}", exceeds_threshold=True, change_pct=float(i))
            for i in range(5)
        ]
        summary = format_webhook_summary(flagged, flagged)
        assert "5 flagged" in summary
