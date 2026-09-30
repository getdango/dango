"""tests/unit/test_schedules_save.py

save_schedules_config(): preserves non-schedule sections, creates missing files,
and refuses to overwrite invalid YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dango.config.exceptions import ConfigValidationError
from dango.config.schedules import (
    ScheduleConfig,
    SchedulesConfig,
    load_schedules_config,
    save_schedules_config,
)

_NOTIF = {"webhooks": [{"name": "team_slack", "url": "https://hooks.example.com/x"}]}


def _cfg() -> SchedulesConfig:
    return SchedulesConfig(
        schedules=[ScheduleConfig(name="daily", cron="0 7 * * *", sources=["orders"])]
    )


@pytest.mark.unit
def test_save_preserves_notifications_section(tmp_path: Path) -> None:
    path = tmp_path / ".dango" / "schedules.yml"
    path.parent.mkdir()
    path.write_text(yaml.dump({"schedules": [], "notifications": _NOTIF}))

    save_schedules_config(tmp_path, _cfg())

    raw = yaml.safe_load(path.read_text())
    assert raw["notifications"] == _NOTIF
    assert [s["name"] for s in raw["schedules"]] == ["daily"]


@pytest.mark.unit
def test_save_creates_file_when_missing(tmp_path: Path) -> None:
    save_schedules_config(tmp_path, _cfg())
    loaded = load_schedules_config(tmp_path)
    assert [s.name for s in loaded.schedules] == ["daily"]


@pytest.mark.unit
def test_save_invalid_existing_yaml_raises_and_leaves_file(tmp_path: Path) -> None:
    path = tmp_path / ".dango" / "schedules.yml"
    path.parent.mkdir()
    path.write_text("schedules: [\n")

    with pytest.raises(ConfigValidationError):
        save_schedules_config(tmp_path, _cfg())

    assert path.read_text() == "schedules: [\n"
