"""tests/unit/test_schedules_config.py

Tests for dango.config.schedules — schedule config models, validation, and reload.
"""

from unittest.mock import MagicMock, patch

import pytest
import yaml

_MOD = "dango.config.schedules"


@pytest.mark.unit
class TestScheduleType:
    """Test ScheduleType enum."""

    def test_enum_members(self):
        from dango.config.schedules import ScheduleType

        assert ScheduleType.SYNC.value == "sync"
        assert ScheduleType.DBT.value == "dbt"

    def test_enum_count(self):
        from dango.config.schedules import ScheduleType

        assert len(ScheduleType) == 2


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
        issues = validate_schedules(scheds, {"csv"})
        assert issues == []

    def test_duplicate_names(self):
        from dango.config.schedules import ScheduleConfig, validate_schedules

        scheds = [
            ScheduleConfig(name="s1", cron="daily", sources=["csv"]),
            ScheduleConfig(name="s1", cron="every_hour", sources=["csv"]),
        ]
        issues = validate_schedules(scheds, {"csv"})
        assert any("Duplicate" in i for i in issues)

    def test_unknown_source(self):
        from dango.config.schedules import ScheduleConfig, validate_schedules

        scheds = [ScheduleConfig(name="s1", cron="daily", sources=["nonexistent"])]
        issues = validate_schedules(scheds, {"csv"})
        assert any("unknown source" in i for i in issues)

    def test_interval_vs_duration_warning(self):
        from dango.config.schedules import ScheduleConfig, validate_schedules

        # every_15m = 900s, avg duration = 2000s → 900 < 2000*0.8 = 1600
        scheds = [ScheduleConfig(name="s1", cron="every_15m", sources=["slow_src"])]
        issues = validate_schedules(scheds, {"slow_src"}, average_durations={"slow_src": 2000.0})
        assert any("less than 80%" in i for i in issues)

    def test_interval_vs_duration_skipped_when_none(self):
        from dango.config.schedules import ScheduleConfig, validate_schedules

        scheds = [ScheduleConfig(name="s1", cron="every_15m", sources=["csv"])]
        issues = validate_schedules(scheds, {"csv"}, average_durations=None)
        # No duration warning when average_durations is None
        assert not any("less than 80%" in i for i in issues)

    def test_overlap_warning(self):
        from dango.config.schedules import ScheduleConfig, validate_schedules

        # Two schedules with identical crons sharing a source
        scheds = [
            ScheduleConfig(name="s1", cron="0 6 * * *", sources=["csv"]),
            ScheduleConfig(name="s2", cron="0 6 * * *", sources=["csv"]),
        ]
        issues = validate_schedules(scheds, {"csv"})
        assert any("overlapping" in i.lower() for i in issues)


@pytest.mark.unit
class TestLogStartupChecks:
    """Test log_startup_checks() logging."""

    def test_empty_schedules_logs_info(self):
        from dango.config.schedules import log_startup_checks

        with patch(f"{_MOD}.logger") as mock_logger:
            log_startup_checks([], {"csv"}, "/tmp/project")

        mock_logger.info.assert_called_once_with("no_schedules_configured")

    def test_unscheduled_sources_logged(self):
        from dango.config.schedules import ScheduleConfig, log_startup_checks

        scheds = [ScheduleConfig(name="s1", cron="daily", sources=["csv"])]
        with (
            patch(f"{_MOD}.logger") as mock_logger,
            patch("dango.config.loader.ConfigLoader") as mock_loader_cls,
        ):
            mock_loader_cls.return_value.load_cloud_config.return_value = None
            log_startup_checks(scheds, {"csv", "stripe"}, "/tmp/project")

        info_calls = mock_logger.info.call_args_list
        assert any(call[0][0] == "unscheduled_sources" for call in info_calls)

    def test_cloud_conflict_logged(self):
        from dango.config.schedules import ScheduleConfig, log_startup_checks

        scheds = [ScheduleConfig(name="s1", cron="daily", sources=["csv"])]
        cloud_cfg = MagicMock()
        cloud_cfg.droplet_id = 12345

        with (
            patch(f"{_MOD}.logger") as mock_logger,
            patch("dango.config.loader.ConfigLoader") as mock_loader_cls,
        ):
            mock_loader_cls.return_value.load_cloud_config.return_value = cloud_cfg
            log_startup_checks(scheds, {"csv"}, "/tmp/project")

        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][0] == "cloud_schedule_conflict"

    def test_all_sources_covered(self):
        from dango.config.schedules import ScheduleConfig, log_startup_checks

        scheds = [ScheduleConfig(name="s1", cron="daily", sources=["csv"])]
        with (
            patch(f"{_MOD}.logger") as mock_logger,
            patch("dango.config.loader.ConfigLoader") as mock_loader_cls,
        ):
            mock_loader_cls.return_value.load_cloud_config.return_value = None
            log_startup_checks(scheds, {"csv"}, "/tmp/project")

        info_calls = mock_logger.info.call_args_list
        # No "unscheduled_sources" log when all are covered
        assert not any(call[0][0] == "unscheduled_sources" for call in info_calls)


@pytest.mark.unit
class TestLoadSchedulesConfig:
    """Test load_schedules_config() file loading."""

    def test_missing_file_returns_empty(self, tmp_path):
        from dango.config.schedules import load_schedules_config

        cfg = load_schedules_config(tmp_path)
        assert cfg.schedules == []

    def test_valid_yaml(self, tmp_path):
        from dango.config.schedules import load_schedules_config

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        data = {
            "schedules": [
                {"name": "daily_sync", "cron": "0 6 * * *", "sources": ["csv"]},
            ]
        }
        (dango_dir / "schedules.yml").write_text(yaml.safe_dump(data))

        cfg = load_schedules_config(tmp_path)
        assert len(cfg.schedules) == 1
        assert cfg.schedules[0].name == "daily_sync"

    def test_invalid_yaml_raises(self, tmp_path):
        from dango.config.exceptions import ConfigValidationError
        from dango.config.schedules import load_schedules_config

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "schedules.yml").write_text("schedules: [unclosed bracket\n")

        with pytest.raises(ConfigValidationError, match="Invalid YAML"):
            load_schedules_config(tmp_path)


@pytest.mark.unit
class TestReloadSchedules:
    """Test reload_schedules() diff and apply logic."""

    def _make_scheduler(self, existing_jobs=None):
        """Create a mock SchedulerService with optional existing jobs."""
        scheduler = MagicMock()
        jobs = []
        if existing_jobs:
            for job_id in existing_jobs:
                job = MagicMock()
                job.id = job_id
                jobs.append(job)
        scheduler.get_jobs.return_value = jobs
        return scheduler

    def test_add_new_job(self):
        from dango.config.schedules import ScheduleConfig, reload_schedules

        scheduler = self._make_scheduler()
        scheds = [ScheduleConfig(name="daily_sync", cron="0 6 * * *", sources=["csv"])]

        result = reload_schedules(scheduler, scheds, "/tmp/project")

        assert "daily_sync" in result.added
        scheduler.add_job.assert_called_once()

    def test_remove_old_job(self):
        from dango.config.schedules import reload_schedules

        scheduler = self._make_scheduler(existing_jobs=["schedule:old_job"])

        result = reload_schedules(scheduler, [], "/tmp/project")

        assert "old_job" in result.removed
        scheduler.remove_job.assert_called_once_with("schedule:old_job")

    def test_update_existing_job(self):
        from dango.config.schedules import ScheduleConfig, reload_schedules

        scheduler = self._make_scheduler(existing_jobs=["schedule:my_sync"])
        scheds = [ScheduleConfig(name="my_sync", cron="0 6 * * *", sources=["csv"])]

        result = reload_schedules(scheduler, scheds, "/tmp/project")

        assert "my_sync" in result.updated
        # remove + re-add for update
        scheduler.remove_job.assert_called_with("schedule:my_sync")
        scheduler.add_job.assert_called_once()

    def test_disabled_schedule_not_added(self):
        from dango.config.schedules import ScheduleConfig, reload_schedules

        scheduler = self._make_scheduler()
        scheds = [
            ScheduleConfig(name="disabled_sync", cron="daily", sources=["csv"], enabled=False)
        ]

        result = reload_schedules(scheduler, scheds, "/tmp/project")

        assert result.added == []
        scheduler.add_job.assert_not_called()

    def test_result_shape(self):
        from dango.config.schedules import ReloadResult, ScheduleConfig, reload_schedules

        scheduler = self._make_scheduler()
        scheds = [ScheduleConfig(name="s1", cron="daily", sources=["csv"])]

        result = reload_schedules(scheduler, scheds, "/tmp/project")

        assert isinstance(result, ReloadResult)
        assert isinstance(result.added, list)
        assert isinstance(result.updated, list)
        assert isinstance(result.removed, list)
        assert isinstance(result.unchanged, list)
