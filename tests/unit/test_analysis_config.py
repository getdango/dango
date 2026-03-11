"""tests/unit/test_analysis_config.py

Tests for metrics config loading and saving (dango/analysis/config.py).
"""

from __future__ import annotations

import pytest

from dango.analysis.config import (
    add_metrics_to_config,
    get_metrics_file_path,
    load_metrics_config,
    save_metrics_config,
)
from dango.analysis.models import ComparisonType, MetricConfig, MetricsConfig
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


def _sample_metric(name: str = "test_metric") -> MetricConfig:
    """Create a sample MetricConfig for testing."""
    return MetricConfig(
        name=name,
        source_table="raw_test.orders",
        value_expression="COUNT(*)",
    )


@pytest.mark.unit
class TestSaveMetricsConfig:
    """save_metrics_config serialization."""

    def test_round_trip(self, tmp_path):
        """Save then load returns equivalent config."""
        original = MetricsConfig(
            enabled=True,
            metrics=[_sample_metric()],
        )
        save_metrics_config(tmp_path, original)
        loaded = load_metrics_config(tmp_path)
        assert loaded.enabled is True
        assert len(loaded.metrics) == 1
        assert loaded.metrics[0].name == "test_metric"

    def test_creates_parent_dirs(self, tmp_path):
        """Creates .dango/ directory if missing."""
        save_metrics_config(tmp_path, MetricsConfig())
        assert (tmp_path / ".dango" / "metrics.yml").exists()

    def test_header_comment(self, tmp_path):
        """Header comment is prepended to the file."""
        save_metrics_config(
            tmp_path,
            MetricsConfig(),
            header_comment="Replace your_table\nwith real name",
        )
        content = (tmp_path / ".dango" / "metrics.yml").read_text()
        assert "# Replace your_table" in content
        assert "# with real name" in content

    def test_excludes_none_fields(self, tmp_path):
        """Fields with None values are excluded from YAML."""
        save_metrics_config(tmp_path, MetricsConfig(metrics=[_sample_metric()]))
        content = (tmp_path / ".dango" / "metrics.yml").read_text()
        assert "filter:" not in content
        assert "warn_threshold:" not in content


@pytest.mark.unit
class TestAddMetricsToConfig:
    """add_metrics_to_config dedup and merge."""

    def test_adds_new_metrics(self, tmp_path):
        """New metrics are appended to empty config."""
        result = add_metrics_to_config(tmp_path, [_sample_metric()])
        assert len(result.metrics) == 1

    def test_dedup_by_name(self, tmp_path):
        """Existing metrics with same name are preserved, not overwritten."""
        add_metrics_to_config(tmp_path, [_sample_metric("revenue")])
        result = add_metrics_to_config(tmp_path, [_sample_metric("revenue")])
        assert len(result.metrics) == 1

    def test_merges_different_names(self, tmp_path):
        """Metrics with different names are both kept."""
        add_metrics_to_config(tmp_path, [_sample_metric("alpha")])
        result = add_metrics_to_config(tmp_path, [_sample_metric("beta")])
        assert len(result.metrics) == 2
        names = {m.name for m in result.metrics}
        assert names == {"alpha", "beta"}
