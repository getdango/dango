"""tests/unit/test_analysis_config.py

Tests for metrics config loading (dango/analysis/config.py).
"""

from __future__ import annotations

import pytest

from dango.analysis.config import get_metrics_file_path, load_metrics_config
from dango.analysis.models import ComparisonType
from dango.exceptions import AnalysisConfigError


@pytest.mark.unit
class TestGetMetricsFilePath:
    """get_metrics_file_path returns the expected path."""

    def test_returns_path(self, tmp_path):
        """Path is .dango/metrics.yml under project root."""
        assert get_metrics_file_path(tmp_path) == tmp_path / ".dango" / "metrics.yml"


@pytest.mark.unit
class TestLoadMetricsConfig:
    """load_metrics_config loading behaviour."""

    def test_missing_file_returns_empty(self, tmp_path):
        """Missing file returns empty MetricsConfig."""
        cfg = load_metrics_config(tmp_path)
        assert cfg.metrics == []
        assert cfg.enabled is True

    def test_empty_file_returns_empty(self, tmp_path):
        """Empty YAML file returns empty MetricsConfig."""
        metrics_dir = tmp_path / ".dango"
        metrics_dir.mkdir()
        (metrics_dir / "metrics.yml").write_text("")
        cfg = load_metrics_config(tmp_path)
        assert cfg.metrics == []

    def test_valid_config(self, tmp_path):
        """Valid YAML loads correctly."""
        metrics_dir = tmp_path / ".dango"
        metrics_dir.mkdir()
        (metrics_dir / "metrics.yml").write_text(
            """\
enabled: true
metrics:
  - name: daily_revenue
    source_table: raw_stripe.payments
    value_expression: "SUM(amount)"
    filter: "status = 'succeeded'"
    compare: week_over_week
    warn_threshold: 10
    drill_down:
      - product_name
      - country
  - name: user_count
    source_table: raw_app.users
    value_expression: "COUNT(*)"
"""
        )
        cfg = load_metrics_config(tmp_path)
        assert cfg.enabled is True
        assert len(cfg.metrics) == 2
        assert cfg.metrics[0].name == "daily_revenue"
        assert cfg.metrics[0].compare == ComparisonType.week_over_week
        assert cfg.metrics[0].warn_threshold == 10.0
        assert cfg.metrics[0].drill_down == ["product_name", "country"]
        assert cfg.metrics[1].name == "user_count"
        assert cfg.metrics[1].filter is None

    def test_disabled_config(self, tmp_path):
        """Config with enabled: false."""
        metrics_dir = tmp_path / ".dango"
        metrics_dir.mkdir()
        (metrics_dir / "metrics.yml").write_text("enabled: false\nmetrics: []\n")
        cfg = load_metrics_config(tmp_path)
        assert cfg.enabled is False

    def test_invalid_yaml_raises(self, tmp_path):
        """Malformed YAML raises AnalysisConfigError."""
        metrics_dir = tmp_path / ".dango"
        metrics_dir.mkdir()
        (metrics_dir / "metrics.yml").write_text("metrics: [invalid: yaml: content\n")
        with pytest.raises(AnalysisConfigError, match="Invalid YAML"):
            load_metrics_config(tmp_path)

    def test_invalid_metric_raises(self, tmp_path):
        """Invalid metric config raises AnalysisConfigError."""
        metrics_dir = tmp_path / ".dango"
        metrics_dir.mkdir()
        (metrics_dir / "metrics.yml").write_text(
            """\
metrics:
  - name: BadName
    source_table: no_dot
    value_expression: "SUM(x)"
"""
        )
        with pytest.raises(AnalysisConfigError, match="Invalid metrics configuration"):
            load_metrics_config(tmp_path)

    def test_no_metrics_key_returns_empty(self, tmp_path):
        """YAML with unrelated keys returns empty MetricsConfig."""
        metrics_dir = tmp_path / ".dango"
        metrics_dir.mkdir()
        (metrics_dir / "metrics.yml").write_text("some_other_key: true\n")
        cfg = load_metrics_config(tmp_path)
        # MetricsConfig accepts extra keys — metrics defaults to []
        assert cfg.metrics == []
