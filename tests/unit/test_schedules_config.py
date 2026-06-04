"""tests/unit/test_schedules_config.py

Tests for dango.config.schedules — models, validators, and cross-validation.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

_MOD = "dango.config.schedules"


@pytest.mark.unit
class TestScheduleType:
    """Test ScheduleType enum."""

    def test_enum_members(self):
        from dango.config.schedules import ScheduleType

        assert ScheduleType.SYNC.value == "sync"
        assert ScheduleType.SYNC_ONLY.value == "sync_only"
        assert ScheduleType.DBT.value == "dbt"

    def test_enum_count(self):
        from dango.config.schedules import ScheduleType

        assert len(ScheduleType) == 3


@pytest.mark.unit
class TestCronPresets:
    """Test CRON_PRESETS dictionary."""

    def test_preset_count(self):
        from dango.config.schedules import CRON_PRESETS

        assert len(CRON_PRESETS) == 5

    def test_all_presets_are_valid_cron(self):
        from croniter import croniter

        from dango.config.schedules import CRON_PRESETS

        for name, expr in CRON_PRESETS.items():
            assert croniter.is_valid(expr), f"Preset {name!r} has invalid cron: {expr!r}"


@pytest.mark.unit
class TestScheduleConfig:
    """Test ScheduleConfig model and validators."""

    def test_valid_sync_config(self):
        from dango.config.schedules import ScheduleConfig

        cfg = ScheduleConfig(name="daily_sync", cron="daily", sources=["google_sheets"])
        assert cfg.name == "daily_sync"
        assert cfg.cron == "0 6 * * *"  # preset resolved
        assert cfg.sources == ["google_sheets"]
        assert cfg.enabled is True

    def test_valid_dbt_config(self):
        from dango.config.schedules import ScheduleConfig, ScheduleType

        cfg = ScheduleConfig(
            name="nightly_dbt",
            type=ScheduleType.DBT,
            cron="0 2 * * *",
            dbt_command="run --select daily_models",
        )
        assert cfg.type == ScheduleType.DBT
        assert cfg.dbt_command == "run --select daily_models"

    def test_preset_resolved(self):
        from dango.config.schedules import ScheduleConfig

        cfg = ScheduleConfig(name="hourly_sync", cron="every_hour", sources=["csv"])
        assert cfg.cron == "0 * * * *"

    def test_invalid_name_rejected(self):
        from dango.config.schedules import ScheduleConfig

        with pytest.raises(Exception, match="lowercase alphanumeric"):
            ScheduleConfig(name="Bad-Name!", cron="daily", sources=["s"])

    def test_name_must_start_with_letter(self):
        from dango.config.schedules import ScheduleConfig

        with pytest.raises(Exception, match="lowercase alphanumeric"):
            ScheduleConfig(name="123bad", cron="daily", sources=["s"])

    def test_invalid_cron_rejected(self):
        from dango.config.schedules import ScheduleConfig

        with pytest.raises(Exception, match="Invalid cron"):
            ScheduleConfig(name="bad_cron", cron="not a cron", sources=["s"])

    def test_invalid_notify_on_rejected(self):
        from dango.config.schedules import ScheduleConfig

        with pytest.raises(Exception, match="Invalid notify_on"):
            ScheduleConfig(name="bad_notify", cron="daily", sources=["s"], notify_on=["invalid"])

    def test_valid_sync_only_config(self):
        from dango.config.schedules import ScheduleConfig, ScheduleType

        cfg = ScheduleConfig(
            name="sync_no_dbt",
            type=ScheduleType.SYNC_ONLY,
            cron="daily",
            sources=["google_sheets"],
        )
        assert cfg.type == ScheduleType.SYNC_ONLY
        assert cfg.sources == ["google_sheets"]
        assert cfg.dbt_command is None

    def test_sync_only_requires_sources(self):
        from dango.config.schedules import ScheduleConfig, ScheduleType

        with pytest.raises(Exception, match="at least one source"):
            ScheduleConfig(
                name="no_sources",
                type=ScheduleType.SYNC_ONLY,
                cron="daily",
                sources=[],
            )

    def test_sync_only_rejects_dbt_command(self):
        from dango.config.schedules import ScheduleConfig, ScheduleType

        with pytest.raises(Exception, match="must not specify a dbt_command"):
            ScheduleConfig(
                name="bad_sync_only",
                type=ScheduleType.SYNC_ONLY,
                cron="daily",
                sources=["csv"],
                dbt_command="run",
            )

    def test_sync_requires_sources(self):
        from dango.config.schedules import ScheduleConfig

        with pytest.raises(Exception, match="at least one source"):
            ScheduleConfig(name="no_sources", cron="daily", sources=[])

    def test_dbt_requires_command(self):
        from dango.config.schedules import ScheduleConfig, ScheduleType

        with pytest.raises(Exception, match="dbt_command"):
            ScheduleConfig(name="no_cmd", type=ScheduleType.DBT, cron="daily")

    def test_defaults(self):
        from dango.config.schedules import ScheduleConfig, ScheduleType

        cfg = ScheduleConfig(name="test_sched", cron="daily", sources=["s"])
        assert cfg.type == ScheduleType.SYNC
        assert cfg.enabled is True
        assert cfg.timezone is None
        assert cfg.start_date is None
        assert cfg.misfire_grace_time is None
        assert cfg.timeout_minutes is None
        assert cfg.notify_on == []
        assert cfg.dbt_command is None

    def test_get_notify_on_dict_empty(self):
        from dango.config.schedules import ScheduleConfig

        cfg = ScheduleConfig(name="test_sched", cron="daily", sources=["s"])
        assert cfg.get_notify_on_dict() is None

    def test_get_notify_on_dict_with_values(self):
        from dango.config.schedules import ScheduleConfig

        cfg = ScheduleConfig(name="test_sched", cron="daily", sources=["s"], notify_on=["failure"])
        result = cfg.get_notify_on_dict()
        assert result == {"on_failure": True, "on_success": False, "on_stale": False}


@pytest.mark.unit
class TestSchedulesConfig:
    """Test SchedulesConfig container."""

    def test_empty_default(self):
        from dango.config.schedules import SchedulesConfig

        cfg = SchedulesConfig()
        assert cfg.schedules == []

    def test_from_list(self):
        from dango.config.schedules import ScheduleConfig, SchedulesConfig

        s = ScheduleConfig(name="my_sync", cron="daily", sources=["csv"])
        cfg = SchedulesConfig(schedules=[s])
        assert len(cfg.schedules) == 1
        assert cfg.schedules[0].name == "my_sync"


@pytest.mark.unit
class TestValidateSchedules:
    """Test validate_schedules() cross-validation."""

    def test_clean_pass(self):
        from dango.config.schedules import ScheduleConfig, validate_schedules

        scheds = [ScheduleConfig(name="s1", cron="daily", sources=["csv"])]
        errors, warnings = validate_schedules(scheds, {"csv"})
        assert errors == []
        assert warnings == []

    def test_duplicate_names(self):
        from dango.config.schedules import ScheduleConfig, validate_schedules

        scheds = [
            ScheduleConfig(name="s1", cron="daily", sources=["csv"]),
            ScheduleConfig(name="s1", cron="every_hour", sources=["csv"]),
        ]
        errors, warnings = validate_schedules(scheds, {"csv"})
        assert any("Duplicate" in e for e in errors)

    def test_unknown_source(self):
        from dango.config.schedules import ScheduleConfig, validate_schedules

        scheds = [ScheduleConfig(name="s1", cron="daily", sources=["nonexistent"])]
        errors, warnings = validate_schedules(scheds, {"csv"})
        assert any("unknown source" in e for e in errors)

    def test_interval_vs_duration_warning(self):
        from dango.config.schedules import ScheduleConfig, validate_schedules

        # every_15m = 900s, avg duration = 2000s → 900 < 2000*0.8 = 1600
        scheds = [ScheduleConfig(name="s1", cron="every_15m", sources=["slow_src"])]
        errors, warnings = validate_schedules(
            scheds, {"slow_src"}, average_durations={"slow_src": 2000.0}
        )
        assert any("less than 80%" in w for w in warnings)

    def test_interval_vs_duration_skipped_when_none(self):
        from dango.config.schedules import ScheduleConfig, validate_schedules

        scheds = [ScheduleConfig(name="s1", cron="every_15m", sources=["csv"])]
        errors, warnings = validate_schedules(scheds, {"csv"}, average_durations=None)
        assert not any("less than 80%" in w for w in warnings)

    def test_interval_vs_duration_skipped_when_empty_dict(self):
        from dango.config.schedules import ScheduleConfig, validate_schedules

        scheds = [ScheduleConfig(name="s1", cron="every_15m", sources=["csv"])]
        errors, warnings = validate_schedules(scheds, {"csv"}, average_durations={})
        assert not any("less than 80%" in w for w in warnings)

    def test_overlap_warning(self):
        from dango.config.schedules import ScheduleConfig, validate_schedules

        # Two schedules with identical crons sharing a source
        scheds = [
            ScheduleConfig(name="s1", cron="0 6 * * *", sources=["csv"]),
            ScheduleConfig(name="s2", cron="0 6 * * *", sources=["csv"]),
        ]
        errors, warnings = validate_schedules(scheds, {"csv"})
        assert any("overlapping" in w.lower() for w in warnings)


@pytest.mark.unit
class TestLogStartupChecks:
    """Test log_startup_checks() logging."""

    def test_empty_schedules_logs_info(self):
        from dango.config.schedules import log_startup_checks

        with patch(f"{_MOD}.logger") as mock_logger:
            log_startup_checks([], {"csv"}, Path("/tmp/project"))

        mock_logger.info.assert_called_once_with("no_schedules_configured")

    def test_unscheduled_sources_logged(self):
        from dango.config.schedules import ScheduleConfig, log_startup_checks

        scheds = [ScheduleConfig(name="s1", cron="daily", sources=["csv"])]
        with (
            patch(f"{_MOD}.logger") as mock_logger,
            patch("dango.config.loader.ConfigLoader") as mock_loader_cls,
        ):
            mock_loader_cls.return_value.load_cloud_config.return_value = None
            log_startup_checks(scheds, {"csv", "stripe"}, Path("/tmp/project"))

        info_calls = mock_logger.info.call_args_list
        assert any(call[0][0] == "unscheduled_sources" for call in info_calls)

    def test_cloud_conflict_logged(self):
        from dango.config.schedules import ScheduleConfig, log_startup_checks

        scheds = [ScheduleConfig(name="s1", cron="daily", sources=["csv"])]

        with (
            patch(f"{_MOD}.logger") as mock_logger,
            patch("dango.config.helpers.is_running_on_cloud", return_value=True),
        ):
            log_startup_checks(scheds, {"csv"}, Path("/tmp/project"))

        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][0] == "cloud_schedule_conflict"

    def test_all_sources_covered(self):
        from dango.config.schedules import ScheduleConfig, log_startup_checks

        scheds = [ScheduleConfig(name="s1", cron="daily", sources=["csv"])]
        with (
            patch(f"{_MOD}.logger") as mock_logger,
            patch("dango.config.helpers.is_running_on_cloud", return_value=False),
        ):
            log_startup_checks(scheds, {"csv"}, Path("/tmp/project"))

        info_calls = mock_logger.info.call_args_list
        # No "unscheduled_sources" log when all are covered
        assert not any(call[0][0] == "unscheduled_sources" for call in info_calls)


@pytest.mark.unit
class TestGetScheduleJobId:
    """Test get_schedule_job_id() helper."""

    def test_returns_prefixed_id(self):
        from dango.config.schedules import get_schedule_job_id

        assert get_schedule_job_id("daily_sync") == "schedule:daily_sync"

    def test_round_trips_with_removeprefix(self):
        from dango.config.schedules import get_schedule_job_id

        job_id = get_schedule_job_id("my_job")
        assert job_id.removeprefix("schedule:") == "my_job"


@pytest.mark.unit
class TestHelpers:
    """Test private helper functions."""

    def test_get_cron_interval_uniform(self):
        """Uniform cron (every hour) returns 3600s."""
        from dango.config.schedules import _get_cron_interval_seconds

        interval = _get_cron_interval_seconds("0 * * * *")
        assert interval == 3600.0

    def test_get_cron_interval_non_uniform(self):
        """Non-uniform cron (6am and 6pm) returns the min gap (12h)."""
        from dango.config.schedules import _get_cron_interval_seconds

        interval = _get_cron_interval_seconds("0 6,18 * * *")
        assert interval == 12 * 3600.0

    def test_detect_overlaps_identical_crons(self):
        """Two schedules with identical crons and shared source produce a warning."""
        from dango.config.schedules import ScheduleConfig, _detect_overlaps

        scheds = [
            ScheduleConfig(name="s1", cron="0 6 * * *", sources=["csv"]),
            ScheduleConfig(name="s2", cron="0 6 * * *", sources=["csv"]),
        ]
        warnings = _detect_overlaps(scheds)
        assert len(warnings) == 1
        assert "overlapping" in warnings[0].lower()

    def test_detect_overlaps_no_shared_source(self):
        """Same cron but different sources -> no overlap warning."""
        from dango.config.schedules import ScheduleConfig, _detect_overlaps

        scheds = [
            ScheduleConfig(name="s1", cron="0 6 * * *", sources=["csv"]),
            ScheduleConfig(name="s2", cron="0 6 * * *", sources=["stripe"]),
        ]
        warnings = _detect_overlaps(scheds)
        assert warnings == []

    def test_detect_overlaps_disabled_ignored(self):
        """Disabled schedules are not checked for overlaps."""
        from dango.config.schedules import ScheduleConfig, _detect_overlaps

        scheds = [
            ScheduleConfig(name="s1", cron="0 6 * * *", sources=["csv"]),
            ScheduleConfig(name="s2", cron="0 6 * * *", sources=["csv"], enabled=False),
        ]
        warnings = _detect_overlaps(scheds)
        assert warnings == []
