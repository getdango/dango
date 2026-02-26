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

        result = reload_schedules(scheduler, scheds, Path("/tmp/project"))

        assert "daily_sync" in result.added
        scheduler.add_job.assert_called_once()

    def test_remove_old_job(self):
        from dango.config.schedules import reload_schedules

        scheduler = self._make_scheduler(existing_jobs=["schedule:old_job"])

        result = reload_schedules(scheduler, [], Path("/tmp/project"))

        assert "old_job" in result.removed
        scheduler.remove_job.assert_called_once_with("schedule:old_job")

    def test_update_existing_job(self):
        from dango.config.schedules import ScheduleConfig, reload_schedules

        scheduler = self._make_scheduler(existing_jobs=["schedule:my_sync"])
        scheds = [ScheduleConfig(name="my_sync", cron="0 6 * * *", sources=["csv"])]

        result = reload_schedules(scheduler, scheds, Path("/tmp/project"))

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
        call_kwargs = scheduler.add_job.call_args
        assert call_kwargs[1]["kwargs"]["select"] == "run --select daily_models"
