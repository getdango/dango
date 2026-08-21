"""tests/unit/test_schedules_loading.py

Tests for schedule config loading, reload logic, and ConfigLoader integration.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml


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

    def test_schedules_key_null(self, tmp_path):
        """YAML with 'schedules: null' returns empty config."""
        from dango.config.schedules import load_schedules_config

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "schedules.yml").write_text("schedules: null\n")

        cfg = load_schedules_config(tmp_path)
        assert cfg.schedules == []

    def test_no_schedules_key(self, tmp_path):
        """YAML without a 'schedules' key returns empty config."""
        from dango.config.schedules import load_schedules_config

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "schedules.yml").write_text("notifications:\n  webhook_url: http://x\n")

        cfg = load_schedules_config(tmp_path)
        assert cfg.schedules == []

    def test_empty_file(self, tmp_path):
        """Empty YAML file returns empty config."""
        from dango.config.schedules import load_schedules_config

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "schedules.yml").write_text("")

        cfg = load_schedules_config(tmp_path)
        assert cfg.schedules == []


@pytest.mark.unit
class TestConfigLoaderSchedules:
    """Test ConfigLoader.load_schedules_config() integration."""

    def test_missing_file_returns_empty(self, tmp_path):
        from dango.config.loader import ConfigLoader

        loader = ConfigLoader(project_root=tmp_path)
        cfg = loader.load_schedules_config()
        assert cfg.schedules == []

    def test_valid_file(self, tmp_path):
        from dango.config.loader import ConfigLoader

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        data = {
            "schedules": [
                {"name": "daily_sync", "cron": "0 6 * * *", "sources": ["csv"]},
            ]
        }
        (dango_dir / "schedules.yml").write_text(yaml.safe_dump(data))

        loader = ConfigLoader(project_root=tmp_path)
        cfg = loader.load_schedules_config()
        assert len(cfg.schedules) == 1
        assert cfg.schedules[0].name == "daily_sync"

    def test_invalid_yaml_raises(self, tmp_path):
        from dango.config.exceptions import ConfigValidationError
        from dango.config.loader import ConfigLoader

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "schedules.yml").write_text("schedules: [unclosed\n")

        loader = ConfigLoader(project_root=tmp_path)
        with pytest.raises(ConfigValidationError, match="Invalid YAML"):
            loader.load_schedules_config()


@pytest.mark.unit
class TestReloadSchedules:
    """Test reload_schedules() diff and apply logic."""

    def _make_scheduler(self, existing_jobs=None):
        """Create a mock SchedulerService with optional existing jobs.

        *existing_jobs* can be a list of job ID strings (trigger defaults
        to a ``0 6 * * *`` cron), a list of ``(job_id, cron_expr)``
        tuples for explicit trigger control, or a list of 3-tuples
        ``(job_id, cron_expr, job_kwargs)`` for explicit kwargs and next_run_time control.

        job_kwargs: dict with optional "kwargs" (job func kwargs) and "next_run_time"
        (datetime or None) keys. Defaults to empty kwargs and None next_run_time.
        """
        from apscheduler.triggers.cron import CronTrigger

        scheduler = MagicMock()
        jobs = []
        if existing_jobs:
            for entry in existing_jobs:
                if isinstance(entry, tuple):
                    if len(entry) == 3:
                        job_id, cron_expr, job_kwargs_config = entry
                    else:
                        job_id, cron_expr = entry
                        job_kwargs_config = {}
                else:
                    job_id = entry
                    cron_expr = "0 6 * * *"
                    job_kwargs_config = {}
                job = MagicMock()
                job.id = job_id
                job.trigger = CronTrigger.from_crontab(cron_expr)
                job.kwargs = job_kwargs_config.get("kwargs", {})
                job.next_run_time = job_kwargs_config.get("next_run_time", None)
                jobs.append(job)
        scheduler.get_jobs.return_value = jobs
        return scheduler

    def test_add_new_job(self):
        from dango.config.schedules import ScheduleConfig, reload_schedules

        scheduler = self._make_scheduler()
        scheds = [ScheduleConfig(name="daily_sync", cron="0 6 * * *", sources=["csv"])]

        result = reload_schedules(scheduler, scheds, Path("/tmp/project"))

        assert "daily_sync" in result.added
        scheduler.add_job.assert_called_once()

    def test_remove_old_job(self):
        from dango.config.schedules import reload_schedules

        scheduler = self._make_scheduler(existing_jobs=["schedule:old_job"])

        result = reload_schedules(scheduler, [], Path("/tmp/project"))

        assert "old_job" in result.removed
        scheduler.remove_job.assert_called_once_with("schedule:old_job")

    def test_unchanged_trigger_preserves_job(self):
        """Job with same trigger and kwargs is left in place (preserves next_run_time)."""
        from dango.config.schedules import ScheduleConfig, reload_schedules

        matching_kwargs = {
            "kwargs": {
                "schedule_name": "my_sync",
                "sources": ["csv"],
                "project_root": "/tmp/project",
                "skip_dbt": False,
            }
        }
        scheduler = self._make_scheduler(
            existing_jobs=[("schedule:my_sync", "0 6 * * *", matching_kwargs)]
        )
        scheds = [ScheduleConfig(name="my_sync", cron="0 6 * * *", sources=["csv"])]

        result = reload_schedules(scheduler, scheds, Path("/tmp/project"))

        assert "my_sync" in result.unchanged
        scheduler.remove_job.assert_not_called()
        scheduler.add_job.assert_not_called()

    def test_changed_trigger_updates_job(self):
        """Job with different trigger is removed and re-added."""
        from dango.config.schedules import ScheduleConfig, reload_schedules

        scheduler = self._make_scheduler(existing_jobs=[("schedule:my_sync", "0 6 * * *")])
        scheds = [ScheduleConfig(name="my_sync", cron="0 12 * * *", sources=["csv"])]

        result = reload_schedules(scheduler, scheds, Path("/tmp/project"))

        assert "my_sync" in result.updated
        scheduler.remove_job.assert_called_with("schedule:my_sync")
        scheduler.add_job.assert_called_once()

    def test_changed_timezone_updates_job(self):
        """Job with same cron but different timezone is removed and re-added."""
        from dango.config.schedules import ScheduleConfig, reload_schedules

        scheduler = self._make_scheduler(existing_jobs=[("schedule:my_sync", "0 6 * * *")])
        scheds = [
            ScheduleConfig(name="my_sync", cron="0 6 * * *", sources=["csv"], timezone="US/Eastern")
        ]

        result = reload_schedules(scheduler, scheds, Path("/tmp/project"))

        assert "my_sync" in result.updated
        scheduler.remove_job.assert_called_with("schedule:my_sync")
        scheduler.add_job.assert_called_once()

    def test_disabled_schedule_not_added(self):
        from dango.config.schedules import ScheduleConfig, reload_schedules

        scheduler = self._make_scheduler()
        scheds = [
            ScheduleConfig(name="disabled_sync", cron="daily", sources=["csv"], enabled=False)
        ]

        result = reload_schedules(scheduler, scheds, Path("/tmp/project"))

        assert result.added == []
        scheduler.add_job.assert_not_called()

    def test_result_shape(self):
        from dango.config.schedules import ReloadResult, ScheduleConfig, reload_schedules

        scheduler = self._make_scheduler()
        scheds = [ScheduleConfig(name="s1", cron="daily", sources=["csv"])]

        result = reload_schedules(scheduler, scheds, Path("/tmp/project"))

        assert isinstance(result, ReloadResult)
        assert isinstance(result.added, list)
        assert isinstance(result.updated, list)
        assert isinstance(result.removed, list)
        assert isinstance(result.unchanged, list)

    def test_dbt_schedule_reload(self):
        """Test reload with a dbt-type schedule."""
        from dango.config.schedules import ScheduleConfig, ScheduleType, reload_schedules

        scheduler = self._make_scheduler()
        scheds = [
            ScheduleConfig(
                name="nightly_dbt",
                type=ScheduleType.DBT,
                cron="0 2 * * *",
                dbt_command="run --select daily_models",
            )
        ]

        result = reload_schedules(scheduler, scheds, Path("/tmp/project"))

        assert "nightly_dbt" in result.added
        scheduler.add_job.assert_called_once()
        _, call_kwargs = scheduler.add_job.call_args
        assert call_kwargs["kwargs"]["dbt_command"] == "run --select daily_models"
        assert call_kwargs["id"] == "schedule:nightly_dbt"

    def test_changed_sources_updates_job(self):
        """Job with different sources is removed and re-added, preserving next_run_time."""
        from datetime import datetime

        from dango.config.schedules import ScheduleConfig, reload_schedules

        old_next_run = datetime.fromisoformat("2025-01-15T06:00:00")
        old_kwargs = {
            "kwargs": {
                "schedule_name": "my_sync",
                "sources": ["old_source"],
                "project_root": "/tmp/project",
                "skip_dbt": False,
            },
            "next_run_time": old_next_run,
        }
        scheduler = self._make_scheduler(
            existing_jobs=[("schedule:my_sync", "0 6 * * *", old_kwargs)]
        )
        scheds = [ScheduleConfig(name="my_sync", cron="0 6 * * *", sources=["new_source"])]

        result = reload_schedules(scheduler, scheds, Path("/tmp/project"))

        assert "my_sync" in result.updated
        scheduler.remove_job.assert_called_once_with("schedule:my_sync")
        scheduler.add_job.assert_called_once()
        # Verify that next_run_time is preserved when only kwargs changed
        _, call_kwargs = scheduler.add_job.call_args
        assert call_kwargs["next_run_time"] == old_next_run

    def test_timeout_change_only_stays_unchanged(self):
        """_timeout_minutes changes are ignored (excluded from kwargs comparison)."""
        from dango.config.schedules import ScheduleConfig, reload_schedules

        old_kwargs = {
            "kwargs": {
                "schedule_name": "my_sync",
                "sources": ["csv"],
                "project_root": "/tmp/project",
                "skip_dbt": False,
                "_timeout_minutes": 30,
            }
        }
        scheduler = self._make_scheduler(
            existing_jobs=[("schedule:my_sync", "0 6 * * *", old_kwargs)]
        )
        scheds = [
            ScheduleConfig(
                name="my_sync",
                cron="0 6 * * *",
                sources=["csv"],
                timeout_minutes=60,
            )
        ]

        result = reload_schedules(scheduler, scheds, Path("/tmp/project"))

        # _timeout_minutes is excluded from comparison, so job stays unchanged
        assert "my_sync" in result.unchanged
        scheduler.remove_job.assert_not_called()
        scheduler.add_job.assert_not_called()

    def test_kwargs_and_trigger_both_changed_updates_job(self):
        """When both trigger and kwargs change, next_run_time is NOT preserved."""
        from datetime import datetime

        from dango.config.schedules import ScheduleConfig, reload_schedules

        old_next_run = datetime.fromisoformat("2025-01-15T06:00:00")
        old_kwargs = {
            "kwargs": {
                "schedule_name": "my_sync",
                "sources": ["old_source"],
                "project_root": "/tmp/project",
                "skip_dbt": False,
            },
            "next_run_time": old_next_run,
        }
        scheduler = self._make_scheduler(
            existing_jobs=[("schedule:my_sync", "0 6 * * *", old_kwargs)]
        )
        scheds = [ScheduleConfig(name="my_sync", cron="0 12 * * *", sources=["new_source"])]

        result = reload_schedules(scheduler, scheds, Path("/tmp/project"))

        assert "my_sync" in result.updated
        scheduler.remove_job.assert_called_once_with("schedule:my_sync")
        scheduler.add_job.assert_called_once()
        # Verify that next_run_time is NOT preserved when trigger also changed
        _, call_kwargs = scheduler.add_job.call_args
        assert "next_run_time" not in call_kwargs
