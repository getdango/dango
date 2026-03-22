"""tests/unit/test_ingestion_ux.py

Tests for Phase 5c ingestion UX improvements (anomaly detection, gap fill,
failing resource identification, duration formatting, cron display).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# _format_duration (static method on DltPipelineRunner)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFormatDuration:
    """Tests for DltPipelineRunner._format_duration."""

    def _fmt(self, seconds: float) -> str:
        from dango.ingestion.dlt_runner import DltPipelineRunner

        return DltPipelineRunner._format_duration(seconds)

    def test_seconds_only(self) -> None:
        assert self._fmt(42) == "42s"

    def test_zero_seconds(self) -> None:
        assert self._fmt(0) == "0s"

    def test_minutes_and_seconds(self) -> None:
        assert self._fmt(125) == "2m 5s"

    def test_exactly_one_minute(self) -> None:
        assert self._fmt(60) == "1m 0s"

    def test_hours_and_minutes(self) -> None:
        assert self._fmt(3720) == "1h 2m"

    def test_fractional_seconds(self) -> None:
        assert self._fmt(12.7) == "13s"


# ---------------------------------------------------------------------------
# _identify_failing_resource (static method on DltPipelineRunner)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIdentifyFailingResource:
    """Tests for DltPipelineRunner._identify_failing_resource."""

    def _identify(self, msg: str) -> str | None:
        from dango.ingestion.dlt_runner import DltPipelineRunner

        return DltPipelineRunner._identify_failing_resource(msg)

    def test_resource_colon_pattern(self) -> None:
        assert self._identify("Error in resource: contacts - 403 Forbidden") == "contacts"

    def test_resource_quoted_pattern(self) -> None:
        assert self._identify("Pipeline execution failed for resource 'deals'") == "deals"

    def test_error_in_pattern(self) -> None:
        assert self._identify("Error in companies: connection timeout") == "companies"

    def test_no_match(self) -> None:
        assert self._identify("Connection refused by server") is None

    def test_empty_string(self) -> None:
        assert self._identify("") is None


# ---------------------------------------------------------------------------
# _check_row_count_anomaly
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckRowCountAnomaly:
    """Tests for DltPipelineRunner._check_row_count_anomaly."""

    def _make_runner(self, tmp_path: Path) -> object:
        """Create a minimal DltPipelineRunner with a project_root."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        runner.project_root = tmp_path
        return runner

    def _write_history(self, tmp_path: Path, source_name: str, entries: list[dict]) -> None:
        history_dir = tmp_path / ".dango" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        with open(history_dir / f"{source_name}.json", "w") as f:
            json.dump(entries, f)

    def test_no_history_returns_none(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        assert runner._check_row_count_anomaly("test_source", 100) is None

    def test_first_successful_sync_returns_none(self, tmp_path: Path) -> None:
        # History has only failed entries — no baseline
        self._write_history(tmp_path, "src", [{"status": "failed", "rows_processed": 0}])
        runner = self._make_runner(tmp_path)
        assert runner._check_row_count_anomaly("src", 100) is None

    def test_zero_rows_after_nonzero_returns_error(self, tmp_path: Path) -> None:
        self._write_history(tmp_path, "src", [{"status": "success", "rows_processed": 500}])
        runner = self._make_runner(tmp_path)
        result = runner._check_row_count_anomaly("src", 0)
        assert result is not None
        assert result["level"] == "error"
        assert "Zero rows" in result["message"]

    def test_large_drop_returns_warning(self, tmp_path: Path) -> None:
        self._write_history(tmp_path, "src", [{"status": "success", "rows_processed": 1000}])
        runner = self._make_runner(tmp_path)
        result = runner._check_row_count_anomaly("src", 400)
        assert result is not None
        assert result["level"] == "warning"
        assert "dropped" in result["message"]

    def test_large_spike_returns_warning(self, tmp_path: Path) -> None:
        self._write_history(tmp_path, "src", [{"status": "success", "rows_processed": 100}])
        runner = self._make_runner(tmp_path)
        result = runner._check_row_count_anomaly("src", 500)
        assert result is not None
        assert result["level"] == "warning"
        assert "spiked" in result["message"]

    def test_normal_change_returns_none(self, tmp_path: Path) -> None:
        self._write_history(tmp_path, "src", [{"status": "success", "rows_processed": 1000}])
        runner = self._make_runner(tmp_path)
        assert runner._check_row_count_anomaly("src", 800) is None

    def test_normal_increase_returns_none(self, tmp_path: Path) -> None:
        self._write_history(tmp_path, "src", [{"status": "success", "rows_processed": 1000}])
        runner = self._make_runner(tmp_path)
        assert runner._check_row_count_anomaly("src", 2500) is None


# ---------------------------------------------------------------------------
# get_earliest_start_date
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetEarliestStartDate:
    """Tests for sync_history.get_earliest_start_date."""

    def _write_history(self, tmp_path: Path, source_name: str, entries: list[dict]) -> None:
        history_dir = tmp_path / ".dango" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        with open(history_dir / f"{source_name}.json", "w") as f:
            json.dump(entries, f)

    def test_no_history_returns_none(self, tmp_path: Path) -> None:
        from dango.utils.sync_history import get_earliest_start_date

        assert get_earliest_start_date(tmp_path, "no_source") is None

    def test_no_successful_entries(self, tmp_path: Path) -> None:
        from dango.utils.sync_history import get_earliest_start_date

        self._write_history(
            tmp_path,
            "src",
            [
                {"status": "failed", "start_date": "2024-01-01"},
            ],
        )
        assert get_earliest_start_date(tmp_path, "src") is None

    def test_returns_earliest_date(self, tmp_path: Path) -> None:
        from dango.utils.sync_history import get_earliest_start_date

        self._write_history(
            tmp_path,
            "src",
            [
                {"status": "success", "start_date": "2024-06-01"},
                {"status": "success", "start_date": "2024-01-15"},
                {"status": "success", "start_date": "2024-03-01"},
            ],
        )
        assert get_earliest_start_date(tmp_path, "src") == "2024-01-15"

    def test_skips_entries_without_start_date(self, tmp_path: Path) -> None:
        from dango.utils.sync_history import get_earliest_start_date

        self._write_history(
            tmp_path,
            "src",
            [
                {"status": "success"},
                {"status": "success", "start_date": "2024-05-01"},
            ],
        )
        assert get_earliest_start_date(tmp_path, "src") == "2024-05-01"


# ---------------------------------------------------------------------------
# _cron_to_display (web/routes/sources.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCronToDisplay:
    """Tests for sources route _cron_to_display helper."""

    def test_known_cron(self) -> None:
        from dango.web.routes.sources import _cron_to_display

        assert _cron_to_display("0 */6 * * *") == "Every 6 hours"

    def test_daily_midnight(self) -> None:
        from dango.web.routes.sources import _cron_to_display

        assert _cron_to_display("0 0 * * *") == "Daily at midnight"

    def test_unknown_cron_returns_raw(self) -> None:
        from dango.web.routes.sources import _cron_to_display

        assert _cron_to_display("*/5 * * * *") == "*/5 * * * *"

    def test_weekly_monday(self) -> None:
        from dango.web.routes.sources import _cron_to_display

        assert _cron_to_display("0 0 * * 1") == "Weekly (Monday)"
