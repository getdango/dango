"""tests/unit/test_cli_sync.py

Unit tests for dango sync CLI backfill and dev-sync options.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from dango.cli.commands.source import (
    _parse_duration,
    _source_supports_date_range,
    sync,
)


@pytest.mark.unit
class TestParseDuration:
    """Tests for _parse_duration() helper."""

    def test_days(self) -> None:
        assert _parse_duration("7d") == 7

    def test_days_large(self) -> None:
        assert _parse_duration("30d") == 30

    def test_weeks(self) -> None:
        assert _parse_duration("2w") == 14

    def test_months(self) -> None:
        assert _parse_duration("1m") == 30

    def test_months_multiple(self) -> None:
        assert _parse_duration("3m") == 90

    def test_case_insensitive(self) -> None:
        assert _parse_duration("7D") == 7
        assert _parse_duration("2W") == 14
        assert _parse_duration("1M") == 30

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(click.BadParameter, match="Invalid duration"):
            _parse_duration("abc")

    def test_zero_raises(self) -> None:
        with pytest.raises(click.BadParameter, match="positive"):
            _parse_duration("0d")


@pytest.mark.unit
class TestSourceSupportsDateRange:
    """Tests for _source_supports_date_range() helper."""

    def test_facebook_ads_supports(self) -> None:
        assert _source_supports_date_range("facebook_ads") is True

    def test_stripe_supports(self) -> None:
        assert _source_supports_date_range("stripe") is True

    def test_google_analytics_supports(self) -> None:
        assert _source_supports_date_range("google_analytics") is True

    def test_csv_not_supported(self) -> None:
        assert _source_supports_date_range("csv") is False

    def test_unknown_source_not_supported(self) -> None:
        assert _source_supports_date_range("nonexistent_source_xyz") is False


def _patch_sync_prereqs():
    """Stack patches for all sync prerequisites (lazy imports)."""
    mock_sources = MagicMock()
    mock_sources.sources = []
    mock_config = MagicMock()
    mock_config.sources = mock_sources

    return [
        patch("dango.cli.utils.require_project_context", return_value=Path("/tmp/fake")),
        patch("dango.utils.DbtLock"),
        patch("dango.cli.utils.check_git_branch_warning"),
        patch("dango.config.get_config", return_value=mock_config),
        patch(
            "dango.cli.commands.source.check_unreferenced_custom_sources",
            return_value=[],
        ),
    ]


@pytest.mark.unit
class TestSyncBackfillValidation:
    """Tests for backfill/limit validation via CliRunner."""

    def _invoke(self, args: list[str]) -> object:
        """Invoke sync command with all prerequisites mocked."""
        runner = CliRunner()
        patches = _patch_sync_prereqs()
        for p in patches:
            p.start()
        try:
            return runner.invoke(sync, args, obj={"project_root": "/tmp"})
        finally:
            for p in patches:
                p.stop()

    def test_backfill_conflicts_with_since(self) -> None:
        """--backfill and --since cannot be used together."""
        result = self._invoke(["--backfill", "30d", "--since", "2024-01-01"])
        assert result.exit_code != 0
        assert "conflicts" in result.output.lower()

    def test_backfill_conflicts_with_until(self) -> None:
        """--backfill and --until cannot be used together."""
        result = self._invoke(["--backfill", "7d", "--until", "2024-12-31"])
        assert result.exit_code != 0
        assert "conflicts" in result.output.lower()

    def test_since_must_be_before_until(self) -> None:
        """--since must be chronologically before --until."""
        result = self._invoke(["--since", "2024-12-31", "--until", "2024-01-01"])
        assert result.exit_code != 0
        assert "before" in result.output.lower()

    def test_negative_limit_rejected(self) -> None:
        """Click's type=int rejects non-numeric --limit."""
        runner = CliRunner()
        result = runner.invoke(sync, ["--limit", "abc"], obj={"project_root": "/tmp"})
        assert result.exit_code != 0

    def test_zero_limit_rejected(self) -> None:
        """--limit 0 is rejected as non-positive."""
        result = self._invoke(["--limit", "0"])
        assert result.exit_code != 0
        assert "positive" in result.output.lower()
