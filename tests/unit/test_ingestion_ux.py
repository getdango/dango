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
    """Tests for DltPipelineRunner._check_row_count_anomaly.

    After BUG-182 fix, anomaly detection compares total DB rows (across all
    tables in raw_{source} schema) against previous sync's total_row_count.
    """

    def _make_runner(self, tmp_path: Path) -> object:
        """Create a minimal DltPipelineRunner with a project_root."""
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        runner.project_root = tmp_path
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        return runner

    def _write_history(self, tmp_path: Path, source_name: str, entries: list[dict]) -> None:
        history_dir = tmp_path / ".dango" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        with open(history_dir / f"{source_name}.json", "w") as f:
            json.dump(entries, f)

    def _setup_db(self, tmp_path: Path, source_name: str, table_rows: dict[str, int]) -> None:
        """Create DuckDB with raw_{source_name} schema and tables with given row counts."""
        import duckdb

        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        schema = f"raw_{source_name}"
        conn = duckdb.connect(str(db_path))
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        for table_name, count in table_rows.items():
            conn.execute(f'CREATE TABLE "{schema}"."{table_name}" (id INTEGER)')
            if count > 0:
                conn.execute(
                    f'INSERT INTO "{schema}"."{table_name}" SELECT unnest(range(1, {count + 1}))'
                )
        conn.close()

    def test_no_db_returns_none(self, tmp_path: Path) -> None:
        """No DuckDB file → graceful degradation, returns None."""
        runner = self._make_runner(tmp_path)
        self._write_history(tmp_path, "src", [{"status": "success", "rows_processed": 500}])
        assert runner._check_row_count_anomaly("src") is None

    def test_no_history_returns_none(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        self._setup_db(tmp_path, "src", {"orders": 100})
        assert runner._check_row_count_anomaly("src") is None

    def test_incremental_no_false_alarm(self, tmp_path: Path) -> None:
        """DB has 68K rows total, history shows 68K previous → no warning."""
        runner = self._make_runner(tmp_path)
        self._setup_db(tmp_path, "src", {"orders": 50000, "customers": 18000})
        self._write_history(tmp_path, "src", [{"status": "success", "total_row_count": 68000}])
        result = runner._check_row_count_anomaly("src")
        assert result is None

    def test_actual_drop_triggers_warning(self, tmp_path: Path) -> None:
        """DB has 400 rows, history shows 1000 total → warning (ratio 0.4 < 0.5)."""
        runner = self._make_runner(tmp_path)
        self._setup_db(tmp_path, "src", {"orders": 250, "customers": 150})
        self._write_history(tmp_path, "src", [{"status": "success", "total_row_count": 1000}])
        result = runner._check_row_count_anomaly("src")
        assert result is not None
        assert result["level"] == "warning"
        assert "dropped" in result["message"]

    def test_zero_rows_error(self, tmp_path: Path) -> None:
        """DB has 0 rows, history shows 500 total → error."""
        runner = self._make_runner(tmp_path)
        self._setup_db(tmp_path, "src", {"orders": 0, "customers": 0})
        self._write_history(tmp_path, "src", [{"status": "success", "total_row_count": 500}])
        result = runner._check_row_count_anomaly("src")
        assert result is not None
        assert result["level"] == "error"
        assert "Zero rows" in result["message"]

    def test_backward_compat_falls_back_to_rows_processed(self, tmp_path: Path) -> None:
        """History lacks total_row_count → falls back to rows_processed."""
        runner = self._make_runner(tmp_path)
        self._setup_db(tmp_path, "src", {"orders": 300})
        self._write_history(tmp_path, "src", [{"status": "success", "rows_processed": 1000}])
        # DB total is 300 vs prev 1000 → >50% drop
        result = runner._check_row_count_anomaly("src")
        assert result is not None
        assert result["level"] == "warning"
        assert "dropped" in result["message"]

    def test_normal_growth_no_alarm(self, tmp_path: Path) -> None:
        """DB has 68004, history shows 68000 → no warning."""
        runner = self._make_runner(tmp_path)
        self._setup_db(tmp_path, "src", {"orders": 50004, "customers": 18000})
        self._write_history(tmp_path, "src", [{"status": "success", "total_row_count": 68000}])
        result = runner._check_row_count_anomaly("src")
        assert result is None

    def test_large_spike_returns_warning(self, tmp_path: Path) -> None:
        """DB total spiked >300% from previous → warning."""
        runner = self._make_runner(tmp_path)
        self._setup_db(tmp_path, "src", {"orders": 4000})
        self._write_history(tmp_path, "src", [{"status": "success", "total_row_count": 1000}])
        result = runner._check_row_count_anomaly("src")
        assert result is not None
        assert result["level"] == "warning"
        assert "spiked" in result["message"]

    def test_total_rows_passed_in(self, tmp_path: Path) -> None:
        """When total_rows is passed, it uses that instead of querying DB."""
        runner = self._make_runner(tmp_path)
        # No DB setup — would return None if it tried to query
        self._write_history(tmp_path, "src", [{"status": "success", "total_row_count": 1000}])
        # Pass total_rows=800 directly — within normal range
        result = runner._check_row_count_anomaly("src", total_rows=800)
        assert result is None

    def test_dlt_internal_tables_excluded(self, tmp_path: Path) -> None:
        """_dlt_ prefixed tables should not be counted in total."""
        import duckdb

        runner = self._make_runner(tmp_path)
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(db_path))
        conn.execute('CREATE SCHEMA IF NOT EXISTS "raw_src"')
        conn.execute('CREATE TABLE "raw_src"."orders" (id INTEGER)')
        conn.execute('INSERT INTO "raw_src"."orders" SELECT unnest(range(1, 101))')
        conn.execute('CREATE TABLE "raw_src"."_dlt_loads" (id INTEGER)')
        conn.execute('INSERT INTO "raw_src"."_dlt_loads" SELECT unnest(range(1, 10001))')
        conn.close()

        total = runner._get_source_total_rows("src")
        assert total == 100  # Only orders, not _dlt_loads


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

    def test_cron_presets_every_hour_variant(self) -> None:
        from dango.web.routes.sources import _cron_to_display

        assert _cron_to_display("0 * * * *") == "Every hour"

    def test_cron_presets_weekly_6am(self) -> None:
        from dango.web.routes.sources import _cron_to_display

        assert _cron_to_display("0 6 * * 1") == "Weekly (Monday at 6 AM)"


# ---------------------------------------------------------------------------
# _suggest_data_path (module-level helper in source_wizard.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSuggestDataPath:
    """Tests for _suggest_data_path helper."""

    def _suggest(self, response: object) -> str | None:
        from dango.cli.source_wizard import _suggest_data_path

        return _suggest_data_path(response)

    def test_top_level_list(self) -> None:
        assert self._suggest([{"id": 1}, {"id": 2}]) is None

    def test_depth_one_list(self) -> None:
        assert self._suggest({"data": [{"id": 1}]}) == "data"

    def test_depth_two_list(self) -> None:
        assert self._suggest({"response": {"items": [{"id": 1}]}}) == "response.items"

    def test_non_dict(self) -> None:
        assert self._suggest("not json") is None

    def test_empty_dict(self) -> None:
        assert self._suggest({}) is None

    def test_dict_no_lists(self) -> None:
        assert self._suggest({"count": 10, "status": "ok"}) is None


# ---------------------------------------------------------------------------
# _suggest_primary_key (module-level helper in source_wizard.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSuggestPrimaryKey:
    """Tests for _suggest_primary_key helper."""

    def _suggest(self, response: object, data_selector: str | None) -> str | None:
        from dango.cli.source_wizard import _suggest_primary_key

        return _suggest_primary_key(response, data_selector)

    def test_id_field(self) -> None:
        assert self._suggest([{"id": 1, "name": "a"}], None) == "id"

    def test_underscore_id_field(self) -> None:
        assert self._suggest([{"user_id": 1, "name": "a"}], None) == "user_id"

    def test_no_matching_fields(self) -> None:
        assert self._suggest([{"name": "a", "value": 1}], None) is None

    def test_with_data_selector(self) -> None:
        resp = {"data": {"items": [{"uuid": "abc", "title": "x"}]}}
        assert self._suggest(resp, "data.items") == "uuid"

    def test_empty_list(self) -> None:
        assert self._suggest([], None) is None
