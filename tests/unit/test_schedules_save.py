"""tests/unit/test_schedules_save.py

save_schedules_config(): preserves non-schedule sections, creates missing files,
refuses to overwrite invalid YAML; plus ScheduleConfig timezone validation.
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


@pytest.mark.unit
class TestTimezoneValidation:
    def _write(self, root: Path, tz_line: str) -> None:
        d = root / ".dango"
        d.mkdir(exist_ok=True)
        (d / "schedules.yml").write_text(
            "schedules:\n  - name: daily\n    cron: '0 7 * * *'\n    sources: [orders]\n" + tz_line
        )

    def test_hand_written_bad_timezone_raises(self, tmp_path: Path) -> None:
        self._write(tmp_path, "    timezone: Mars/Olympus\n")
        with pytest.raises(ConfigValidationError, match="Mars/Olympus"):
            load_schedules_config(tmp_path)

    def test_valid_iana_timezone_loads(self, tmp_path: Path) -> None:
        self._write(tmp_path, "    timezone: Asia/Singapore\n")
        assert load_schedules_config(tmp_path).schedules[0].timezone == "Asia/Singapore"

    def test_missing_timezone_loads_as_none(self, tmp_path: Path) -> None:
        self._write(tmp_path, "")
        assert load_schedules_config(tmp_path).schedules[0].timezone is None
