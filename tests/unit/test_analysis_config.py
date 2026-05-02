"""tests/unit/test_analysis_config.py

Tests for monitors config loading and saving (dango/analysis/config.py).
"""

from __future__ import annotations

import pytest

from dango.analysis.config import (
    add_monitors_to_config,
    get_monitors_file_path,
    load_monitors_config,
    save_monitors_config,
)
from dango.analysis.models import ComparisonType, MonitorConfig, MonitorsConfig
from dango.exceptions import AnalysisConfigError


@pytest.mark.unit
class TestGetMonitorsFilePath:
    """get_monitors_file_path returns the expected path."""

    def test_returns_monitors_path(self, tmp_path):
        """Path is .dango/monitors.yml for new projects."""
        assert get_monitors_file_path(tmp_path) == tmp_path / ".dango" / "monitors.yml"

    def test_falls_back_to_metrics_yml(self, tmp_path):
        """Falls back to .dango/metrics.yml if it exists."""
        metrics_dir = tmp_path / ".dango"
        metrics_dir.mkdir()
        (metrics_dir / "metrics.yml").write_text("enabled: true\n")
        assert get_monitors_file_path(tmp_path) == tmp_path / ".dango" / "metrics.yml"

    def test_prefers_monitors_yml(self, tmp_path):
        """Prefers monitors.yml when both exist."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "metrics.yml").write_text("enabled: true\n")
        (dango_dir / "monitors.yml").write_text("enabled: true\n")
        assert get_monitors_file_path(tmp_path) == tmp_path / ".dango" / "monitors.yml"


@pytest.mark.unit
class TestLoadMonitorsConfig:
    """load_monitors_config loading behaviour."""

    def test_missing_file_returns_empty(self, tmp_path):
        """Missing file returns empty MonitorsConfig."""
        cfg = load_monitors_config(tmp_path)
        assert cfg.monitors == []
        assert cfg.enabled is True

    def test_empty_file_returns_empty(self, tmp_path):
        """Empty YAML file returns empty MonitorsConfig."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "monitors.yml").write_text("")
        cfg = load_monitors_config(tmp_path)
        assert cfg.monitors == []

    def test_valid_config(self, tmp_path):
        """Valid YAML loads correctly."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "monitors.yml").write_text(
            """\
enabled: true
monitors:
  - name: daily_revenue
    source_table: raw_stripe.payments
    value_expression: "SUM(amount)"
    filter: "status = 'succeeded'"
    compare: week_over_week
    alert_threshold: 10
    drill_down:
      - product_name
      - country
  - name: user_count
    source_table: raw_app.users
    value_expression: "COUNT(*)"
"""
        )
        cfg = load_monitors_config(tmp_path)
        assert cfg.enabled is True
        assert len(cfg.monitors) == 2
        assert cfg.monitors[0].name == "daily_revenue"
        assert cfg.monitors[0].compare == ComparisonType.week_over_week
        assert cfg.monitors[0].alert_threshold == 10.0
        assert cfg.monitors[0].drill_down == ["product_name", "country"]
        assert cfg.monitors[1].name == "user_count"
        assert cfg.monitors[1].filter is None

    def test_legacy_metrics_yml_loads(self, tmp_path):
        """Legacy .dango/metrics.yml with old field names loads correctly."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "metrics.yml").write_text(
            """\
enabled: true
metrics:
  - name: daily_revenue
    source_table: raw_stripe.payments
    value_expression: "SUM(amount)"
    warn_threshold: 10
"""
        )
        cfg = load_monitors_config(tmp_path)
        assert len(cfg.monitors) == 1
        assert cfg.monitors[0].alert_threshold == 10.0

    def test_disabled_config(self, tmp_path):
        """Config with enabled: false."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "monitors.yml").write_text("enabled: false\nmonitors: []\n")
        cfg = load_monitors_config(tmp_path)
        assert cfg.enabled is False

    def test_invalid_yaml_raises(self, tmp_path):
        """Malformed YAML raises AnalysisConfigError."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "monitors.yml").write_text("monitors: [invalid: yaml: content\n")
        with pytest.raises(AnalysisConfigError, match="Invalid YAML"):
            load_monitors_config(tmp_path)

    def test_invalid_monitor_raises(self, tmp_path):
        """Invalid monitor config raises AnalysisConfigError."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "monitors.yml").write_text(
            """\
monitors:
  - name: BadName
    source_table: no_dot
    value_expression: "SUM(x)"
"""
        )
        with pytest.raises(AnalysisConfigError, match="Invalid monitors configuration"):
            load_monitors_config(tmp_path)

    def test_no_monitors_key_returns_empty(self, tmp_path):
        """YAML with unrelated keys returns empty MonitorsConfig."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "monitors.yml").write_text("some_other_key: true\n")
        cfg = load_monitors_config(tmp_path)
        assert cfg.monitors == []


def _sample_monitor(name: str = "test_monitor") -> MonitorConfig:
    """Create a sample MonitorConfig for testing."""
    return MonitorConfig(
        name=name,
        source_table="raw_test.orders",
        value_expression="COUNT(*)",
    )


@pytest.mark.unit
class TestSaveMonitorsConfig:
    """save_monitors_config serialization."""

    def test_round_trip(self, tmp_path):
        """Save then load returns equivalent config."""
        original = MonitorsConfig(
            enabled=True,
            monitors=[_sample_monitor()],
        )
        save_monitors_config(tmp_path, original)
        loaded = load_monitors_config(tmp_path)
        assert loaded.enabled is True
        assert len(loaded.monitors) == 1
        assert loaded.monitors[0].name == "test_monitor"

    def test_creates_parent_dirs(self, tmp_path):
        """Creates .dango/ directory if missing."""
        save_monitors_config(tmp_path, MonitorsConfig())
        assert (tmp_path / ".dango" / "monitors.yml").exists()

    def test_header_comment(self, tmp_path):
        """Header comment is prepended to the file."""
        save_monitors_config(
            tmp_path,
            MonitorsConfig(),
            header_comment="Replace your_table\nwith real name",
        )
        content = (tmp_path / ".dango" / "monitors.yml").read_text()
        assert "# Replace your_table" in content
        assert "# with real name" in content

    def test_excludes_none_fields(self, tmp_path):
        """Fields with None values are excluded from YAML."""
        save_monitors_config(tmp_path, MonitorsConfig(monitors=[_sample_monitor()]))
        content = (tmp_path / ".dango" / "monitors.yml").read_text()
        assert "filter:" not in content
        assert "alert_threshold:" not in content

    def test_saves_as_monitors_yml(self, tmp_path):
        """New saves always write to monitors.yml, not metrics.yml."""
        save_monitors_config(tmp_path, MonitorsConfig())
        assert (tmp_path / ".dango" / "monitors.yml").exists()
        assert not (tmp_path / ".dango" / "metrics.yml").exists()


@pytest.mark.unit
class TestAddMonitorsToConfig:
    """add_monitors_to_config dedup and merge."""

    def test_adds_new_monitors(self, tmp_path):
        """New monitors are appended to empty config."""
        result = add_monitors_to_config(tmp_path, [_sample_monitor()])
        assert len(result.monitors) == 1

    def test_dedup_by_name(self, tmp_path):
        """Existing monitors with same name are preserved, not overwritten."""
        add_monitors_to_config(tmp_path, [_sample_monitor("revenue")])
        result = add_monitors_to_config(tmp_path, [_sample_monitor("revenue")])
        assert len(result.monitors) == 1

    def test_merges_different_names(self, tmp_path):
        """Monitors with different names are both kept."""
        add_monitors_to_config(tmp_path, [_sample_monitor("alpha")])
        result = add_monitors_to_config(tmp_path, [_sample_monitor("beta")])
        assert len(result.monitors) == 2
        names = {m.name for m in result.monitors}
        assert names == {"alpha", "beta"}
