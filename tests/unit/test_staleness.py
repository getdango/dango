"""tests/unit/test_staleness.py

Tests for dango.web.helpers — source staleness detection (QG-005).
"""

from unittest.mock import patch

import pytest


class TestGetStalenessThresholdHours:
    """Tests for _get_staleness_threshold_hours()."""

    @staticmethod
    def _make_schedule(name, cron, sources, enabled=True):
        """Build a mock ScheduleConfig."""
        from dango.config.schedules import ScheduleConfig

        return ScheduleConfig(name=name, cron=cron, sources=sources, enabled=enabled)

    @staticmethod
    def _make_config(*schedules):
        """Build a mock SchedulesConfig with the given schedules."""
        from dango.config.schedules import SchedulesConfig

        return SchedulesConfig(schedules=list(schedules))

    def test_returns_none_when_config_load_fails(self):
        """When load_schedules_config raises, return None."""
        with (
            patch(
                "dango.config.schedules.load_schedules_config",
                side_effect=OSError("no file"),
            ),
            patch("dango.web.helpers.get_project_root"),
        ):
            from dango.web.helpers import _get_staleness_threshold_hours

            assert _get_staleness_threshold_hours("any_source") is None

    def test_returns_none_for_unscheduled_source(self):
        """Source not in any schedule returns None (never stale)."""
        config = self._make_config(
            self._make_schedule("daily_sync", "0 6 * * *", ["other_source"]),
        )
        with (
            patch("dango.config.schedules.load_schedules_config", return_value=config),
            patch("dango.web.helpers.get_project_root"),
        ):
            from dango.web.helpers import _get_staleness_threshold_hours

            assert _get_staleness_threshold_hours("my_source") is None

    def test_skips_disabled_schedules(self):
        """Disabled schedules are excluded from staleness calculation."""
        config = self._make_config(
            self._make_schedule("enabled", "0 */6 * * *", ["my_source"], enabled=True),
            self._make_schedule("disabled", "*/5 * * * *", ["my_source"], enabled=False),
        )
        with (
            patch("dango.config.schedules.load_schedules_config", return_value=config),
            patch("dango.web.helpers.get_project_root"),
        ):
            from dango.web.helpers import _get_staleness_threshold_hours

            # Only the enabled schedule matters: 6h interval → 12h threshold
            assert _get_staleness_threshold_hours("my_source") == pytest.approx(12.0)

    def test_returns_two_times_daily_interval(self):
        """Daily cron (24h) → threshold = 48h."""
        config = self._make_config(
            self._make_schedule("daily_sync", "0 6 * * *", ["my_source"]),
        )
        with (
            patch("dango.config.schedules.load_schedules_config", return_value=config),
            patch("dango.web.helpers.get_project_root"),
        ):
            from dango.web.helpers import _get_staleness_threshold_hours

            threshold = _get_staleness_threshold_hours("my_source")
            assert threshold == pytest.approx(48.0)

    def test_returns_two_times_hourly_interval(self):
        """Hourly cron → threshold = 2h."""
        config = self._make_config(
            self._make_schedule("hourly_sync", "0 * * * *", ["my_source"]),
        )
        with (
            patch("dango.config.schedules.load_schedules_config", return_value=config),
            patch("dango.web.helpers.get_project_root"),
        ):
            from dango.web.helpers import _get_staleness_threshold_hours

            threshold = _get_staleness_threshold_hours("my_source")
            assert threshold == pytest.approx(2.0)

    def test_falls_back_to_24h_for_unresolvable_cron(self):
        """When get_cron_interval_seconds returns None, use 24h fallback."""
        config = self._make_config(
            self._make_schedule("complex", "0 6,18 * * 1-5", ["my_source"]),
        )
        with (
            patch("dango.config.schedules.load_schedules_config", return_value=config),
            patch("dango.config.schedules.get_cron_interval_seconds", return_value=None),
            patch("dango.web.helpers.get_project_root"),
        ):
            from dango.web.helpers import _get_staleness_threshold_hours

            # 24h fallback → 2x = 48h threshold
            assert _get_staleness_threshold_hours("my_source") == pytest.approx(48.0)

    def test_falls_back_to_24h_when_cron_raises(self):
        """When get_cron_interval_seconds raises, use 24h fallback."""
        config = self._make_config(
            self._make_schedule("daily", "0 6 * * *", ["my_source"]),
        )
        with (
            patch("dango.config.schedules.load_schedules_config", return_value=config),
            patch(
                "dango.config.schedules.get_cron_interval_seconds",
                side_effect=ValueError("bad cron"),
            ),
            patch("dango.web.helpers.get_project_root"),
        ):
            from dango.web.helpers import _get_staleness_threshold_hours

            assert _get_staleness_threshold_hours("my_source") == pytest.approx(48.0)

    def test_uses_minimum_interval_when_multiple_schedules(self):
        """Source in a daily and an hourly schedule → use hourly (2x 1h = 2h)."""
        config = self._make_config(
            self._make_schedule("daily_sync", "0 6 * * *", ["my_source"]),
            self._make_schedule("hourly_sync", "0 * * * *", ["my_source"]),
        )
        with (
            patch("dango.config.schedules.load_schedules_config", return_value=config),
            patch("dango.web.helpers.get_project_root"),
        ):
            from dango.web.helpers import _get_staleness_threshold_hours

            # Shortest interval is 1h → 2x = 2h
            threshold = _get_staleness_threshold_hours("my_source")
            assert threshold == pytest.approx(2.0)


class TestBuildStalenessThresholds:
    """Tests for build_staleness_thresholds()."""

    @staticmethod
    def _make_schedule(name, cron, sources, enabled=True):
        from dango.config.schedules import ScheduleConfig

        return ScheduleConfig(name=name, cron=cron, sources=sources, enabled=enabled)

    @staticmethod
    def _make_config(*schedules):
        from dango.config.schedules import SchedulesConfig

        return SchedulesConfig(schedules=list(schedules))

    def test_returns_empty_dict_when_config_load_fails(self):
        with (
            patch(
                "dango.config.schedules.load_schedules_config",
                side_effect=OSError("no file"),
            ),
            patch("dango.web.helpers.get_project_root"),
        ):
            from dango.web.helpers import build_staleness_thresholds

            assert build_staleness_thresholds() == {}

    def test_returns_thresholds_for_all_scheduled_sources(self):
        config = self._make_config(
            self._make_schedule("daily_sync", "0 6 * * *", ["src_a"]),
            self._make_schedule("hourly_sync", "0 * * * *", ["src_b"]),
        )
        with (
            patch("dango.config.schedules.load_schedules_config", return_value=config),
            patch("dango.web.helpers.get_project_root"),
        ):
            from dango.web.helpers import build_staleness_thresholds

            result = build_staleness_thresholds()
            assert "src_a" in result
            assert result["src_a"] == pytest.approx(48.0)  # daily → 48h
            assert "src_b" in result
            assert result["src_b"] == pytest.approx(2.0)  # hourly → 2h

    def test_excludes_unscheduled_sources(self):
        config = self._make_config(
            self._make_schedule("daily_sync", "0 6 * * *", ["src_a"]),
        )
        with (
            patch("dango.config.schedules.load_schedules_config", return_value=config),
            patch("dango.web.helpers.get_project_root"),
        ):
            from dango.web.helpers import build_staleness_thresholds

            result = build_staleness_thresholds()
            assert "src_b" not in result  # not in any schedule


class TestGetSourceStatusDataStaleness:
    """Tests for get_source_status_data() with stale sources (QG-005)."""

    def test_staleness_branch_no_logger_crash(self):
        import asyncio

        """When source is stale, logger.info is called without stdlib TypeError.

        Before fix, this would crash with:
        TypeError: Logger._log() got an unexpected keyword argument 'source'
        """
        source = {
            "name": "my_source",
            "type": "google_ads",
            "enabled": True,
        }

        # Mock all the helpers called by get_source_status_data()
        with (
            patch(
                "dango.web.helpers.get_source_tables_info",
                return_value={"total_rows": 100, "has_multiple_tables": False, "tables": []},
            ),
            patch("dango.web.helpers.get_last_sync_time", return_value="2026-08-18T00:00:00"),
            patch("dango.web.helpers.get_last_sync_status", return_value="success"),
            patch(
                "dango.web.helpers.load_sync_history",
                return_value=[
                    {
                        "rows_processed": 100,
                        "duration_seconds": 5,
                        "status": "success",
                        "full_refresh": False,
                    }
                ],
            ),
            patch("dango.web.helpers.get_source_freshness", return_value={"hours_since_sync": 50}),
            # Lazy imports from registry
            patch("dango.ingestion.sources.registry.get_source_capabilities", return_value=None),
            patch("dango.ingestion.sources.registry.get_source_metadata", return_value=None),
        ):
            from dango.web.helpers import get_source_status_data

            # With hours_since_sync=50 and threshold=24, this triggers the staleness branch
            result = asyncio.run(
                get_source_status_data(
                    source,
                    staleness_thresholds={"my_source": 24.0},
                )
            )

            # Should return stale status without crashing
            assert result.status == "stale"
