"""tests/unit/test_analysis_models.py

Tests for analysis Pydantic models (dango/analysis/models.py).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dango.analysis.models import (
    AnalysisResult,
    ComparisonResult,
    ComparisonType,
    MetricConfig,
    MetricsConfig,
    MetricValue,
    MonitorConfig,
    MonitorsConfig,
)


@pytest.mark.unit
class TestComparisonType:
    """ComparisonType enum values."""

    def test_all_values(self):
        """All five comparison types exist."""
        assert ComparisonType.none == "none"
        assert ComparisonType.week_over_week == "week_over_week"
        assert ComparisonType.rolling_7day_avg == "rolling_7day_avg"
        assert ComparisonType.rolling_30day_avg == "rolling_30day_avg"
        assert ComparisonType.prior_period == "prior_period"


@pytest.mark.unit
class TestMonitorConfig:
    """MonitorConfig validation."""

    def test_valid_config(self):
        """Valid config creates successfully."""
        mc = MonitorConfig(
            name="daily_revenue",
            source_table="raw_stripe.payments",
            value_expression="SUM(amount)",
        )
        assert mc.name == "daily_revenue"
        assert mc.compare == ComparisonType.week_over_week
        assert mc.drill_down == []

    def test_full_config(self):
        """Config with all optional fields."""
        mc = MonitorConfig(
            name="daily_revenue",
            source_table="raw_stripe.payments",
            value_expression="SUM(amount)",
            filter="status = 'succeeded'",
            compare=ComparisonType.rolling_7day_avg,
            alert_threshold=10.0,
            drill_down=["product_name", "country"],
        )
        assert mc.filter == "status = 'succeeded'"
        assert mc.alert_threshold == 10.0
        assert mc.drill_down == ["product_name", "country"]

    def test_name_must_be_slug(self):
        """Name with uppercase or special chars is rejected."""
        with pytest.raises(ValidationError, match="lowercase alphanumeric"):
            MonitorConfig(
                name="DailyRevenue",
                source_table="raw.t",
                value_expression="SUM(x)",
            )

    def test_name_must_start_with_letter(self):
        """Name starting with digit is rejected."""
        with pytest.raises(ValidationError, match="lowercase alphanumeric"):
            MonitorConfig(
                name="1metric",
                source_table="raw.t",
                value_expression="SUM(x)",
            )

    def test_source_table_must_have_dot(self):
        """source_table without schema qualifier is rejected."""
        with pytest.raises(ValidationError, match="schema-qualified"):
            MonitorConfig(
                name="my_metric",
                source_table="payments",
                value_expression="SUM(amount)",
            )

    def test_value_expression_not_empty(self):
        """Empty value_expression is rejected."""
        with pytest.raises(ValidationError, match="must not be empty"):
            MonitorConfig(
                name="my_metric",
                source_table="raw.t",
                value_expression="   ",
            )

    def test_alert_threshold_must_be_positive(self):
        """Non-positive alert_threshold is rejected."""
        with pytest.raises(ValidationError, match="must be positive"):
            MonitorConfig(
                name="my_metric",
                source_table="raw.t",
                value_expression="SUM(x)",
                alert_threshold=-5.0,
            )

    def test_alert_threshold_zero_rejected(self):
        """Zero alert_threshold is rejected."""
        with pytest.raises(ValidationError, match="must be positive"):
            MonitorConfig(
                name="my_metric",
                source_table="raw.t",
                value_expression="SUM(x)",
                alert_threshold=0,
            )

    def test_frozen(self):
        """MonitorConfig is immutable."""
        mc = MonitorConfig(name="m", source_table="s.t", value_expression="COUNT(*)")
        with pytest.raises(ValidationError):
            mc.name = "other"


@pytest.mark.unit
class TestMonitorsConfig:
    """MonitorsConfig model."""

    def test_empty_default(self):
        """Empty config has no monitors and is enabled."""
        cfg = MonitorsConfig()
        assert cfg.monitors == []
        assert cfg.enabled is True

    def test_with_monitors(self):
        """Config with a list of monitors."""
        cfg = MonitorsConfig(
            monitors=[
                MonitorConfig(
                    name="m1",
                    source_table="s.t",
                    value_expression="COUNT(*)",
                ),
            ],
        )
        assert len(cfg.monitors) == 1

    def test_disabled(self):
        """Config can be disabled."""
        cfg = MonitorsConfig(enabled=False)
        assert cfg.enabled is False


@pytest.mark.unit
class TestBackwardCompatibility:
    """Backward-compatible aliases work correctly."""

    def test_metric_config_alias(self):
        """MetricConfig is an alias for MonitorConfig."""
        assert MetricConfig is MonitorConfig

    def test_metrics_config_alias(self):
        """MetricsConfig is an alias for MonitorsConfig."""
        assert MetricsConfig is MonitorsConfig

    def test_warn_threshold_accepted(self):
        """Old ``warn_threshold`` field name is accepted via AliasChoices."""
        mc = MonitorConfig(
            name="test",
            source_table="s.t",
            value_expression="COUNT(*)",
            warn_threshold=10.0,
        )
        assert mc.alert_threshold == 10.0

    def test_metrics_key_accepted(self):
        """Old ``metrics`` key is accepted via AliasChoices."""
        cfg = MonitorsConfig(
            metrics=[
                MonitorConfig(
                    name="m1",
                    source_table="s.t",
                    value_expression="COUNT(*)",
                ),
            ],
        )
        assert len(cfg.monitors) == 1


@pytest.mark.unit
class TestMetricValue:
    """MetricValue model."""

    def test_success_value(self):
        """Value with no error."""
        mv = MetricValue(metric_name="m", value=42.5)
        assert mv.value == 42.5
        assert mv.error is None

    def test_error_value(self):
        """Value with error has no value."""
        mv = MetricValue(metric_name="m", error="table not found")
        assert mv.value is None
        assert mv.error == "table not found"


@pytest.mark.unit
class TestComparisonResult:
    """ComparisonResult model."""

    def test_defaults(self):
        """Default comparison has no threshold exceeded."""
        cr = ComparisonResult(
            metric_name="m",
            comparison_type=ComparisonType.week_over_week,
        )
        assert cr.exceeds_threshold is False
        assert cr.change_pct is None

    def test_full_result(self):
        """Full comparison result with all fields."""
        cr = ComparisonResult(
            metric_name="m",
            comparison_type=ComparisonType.rolling_7day_avg,
            current_value=100.0,
            baseline_value=90.0,
            change_pct=11.1,
            exceeds_threshold=True,
            trend_slope=0.5,
            trend_direction="increasing",
            forecast_threshold_days=30,
        )
        assert cr.exceeds_threshold is True
        assert cr.trend_direction == "increasing"


@pytest.mark.unit
class TestAnalysisResult:
    """AnalysisResult model."""

    def test_metric_only(self):
        """Result with no comparison."""
        ar = AnalysisResult(
            metric=MetricValue(metric_name="m", value=10.0),
        )
        assert ar.comparison is None

    def test_with_comparison(self):
        """Result with comparison."""
        ar = AnalysisResult(
            metric=MetricValue(metric_name="m", value=10.0),
            comparison=ComparisonResult(
                metric_name="m",
                comparison_type=ComparisonType.prior_period,
            ),
        )
        assert ar.comparison is not None
