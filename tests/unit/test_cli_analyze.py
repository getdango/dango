"""tests/unit/test_cli_analyze.py

Tests for dango/cli/commands/analyze.py — CLI analyze command.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.main import cli


def _make_mock_result(
    name: str = "revenue",
    value: float = 100.0,
    change_pct: float = 5.0,
    exceeds_threshold: bool = False,
    trend_direction: str | None = None,
    error: str | None = None,
) -> MagicMock:
    """Build a mock AnalysisResult."""
    mock = MagicMock()
    mock.metric.metric_name = name
    mock.metric.value = value
    mock.metric.error = error
    mock.metric.source = "raw_stripe"
    mock.metric.table_name = "payments"
    mock.comparison = MagicMock() if error is None else None
    if mock.comparison:
        mock.comparison.change_pct = change_pct
        mock.comparison.comparison_type.value = "week_over_week"
        mock.comparison.baseline_value = 80.0
        mock.comparison.exceeds_threshold = exceeds_threshold
        mock.comparison.trend_direction = trend_direction
        mock.comparison.trend_slope = None
        mock.comparison.forecast_threshold_days = None
        mock.comparison.current_value = value
    mock.drill_down = []
    return mock


@pytest.mark.unit
class TestAnalyzeCommand:
    """Tests for ``dango analyze``."""

    @patch("dango.analysis.metrics.run_analysis")
    @patch("dango.cli.utils.require_project_context")
    def test_no_results(self, mock_ctx: MagicMock, mock_run: MagicMock) -> None:
        """Empty results show informational message."""
        mock_ctx.return_value = Path("/fake/project")
        mock_run.return_value = []

        runner = CliRunner()
        result = runner.invoke(cli, ["analyze"])
        assert result.exit_code == 0
        assert "No metrics" in result.output

    @patch("dango.analysis.metrics.run_analysis")
    @patch("dango.cli.utils.require_project_context")
    def test_with_results(self, mock_ctx: MagicMock, mock_run: MagicMock) -> None:
        """Results display in a Rich table."""
        mock_ctx.return_value = Path("/fake/project")
        mock_run.return_value = [_make_mock_result()]

        runner = CliRunner()
        result = runner.invoke(cli, ["analyze"])
        assert result.exit_code == 0
        assert "revenue" in result.output

    @patch("dango.analysis.metrics.run_analysis")
    @patch("dango.cli.utils.require_project_context")
    def test_source_filter(self, mock_ctx: MagicMock, mock_run: MagicMock) -> None:
        """--source flag passes source_filter to run_analysis."""
        mock_ctx.return_value = Path("/fake/project")
        mock_run.return_value = []

        runner = CliRunner()
        result = runner.invoke(cli, ["analyze", "--source", "stripe"])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(Path("/fake/project"), source_filter=["raw_stripe"])

    @patch("dango.analysis.metrics.run_analysis")
    @patch("dango.cli.utils.require_project_context")
    def test_error_result_displays(self, mock_ctx: MagicMock, mock_run: MagicMock) -> None:
        """Error results show in table with 'error' status."""
        mock_ctx.return_value = Path("/fake/project")
        mock_run.return_value = [_make_mock_result(name="broken", error="SQL error", value=0.0)]

        runner = CliRunner()
        result = runner.invoke(cli, ["analyze"])
        assert result.exit_code == 0
        assert "broken" in result.output
