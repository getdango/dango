"""tests/unit/test_schedule_cli_status.py

Tests for compute_next_runs() timezone handling and the CLI schedule status/reload
helpers: project-identity gating, live scheduler state, and timezone-aware next runs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from dango.cli.main import cli
from dango.config.schedules import ScheduleConfig, compute_next_runs


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _sched(**kw: Any) -> ScheduleConfig:
    base: dict[str, Any] = {"name": "daily", "cron": "0 7 * * *", "sources": ["a"]}
    base.update(kw)
    return ScheduleConfig(**base)


def _project(tmp_path: Path, schedules: list[dict[str, Any]]) -> Path:
    d = tmp_path / ".dango"
    d.mkdir(parents=True, exist_ok=True)
    (d / "project.yml").write_text("project:\n  name: test\n  version: '1.0'\n")
    (d / "schedules.yml").write_text(yaml.safe_dump({"schedules": schedules}))
    return tmp_path


@pytest.mark.unit
def test_compute_next_runs_honours_timezone() -> None:
    runs = compute_next_runs(_sched(timezone="America/New_York"), count=3)
    assert len(runs) == 3
    assert all(r.hour == 7 and r.minute == 0 for r in runs)
    assert runs[0].tzinfo is not None
    assert runs[0].utcoffset() in (
        __import__("datetime").timedelta(hours=-4),
        __import__("datetime").timedelta(hours=-5),
    )
    assert runs[0] < runs[1] < runs[2]


@pytest.mark.unit
def test_compute_next_runs_disabled_and_invalid() -> None:
    assert compute_next_runs(_sched(enabled=False)) == []
    bad_cron = ScheduleConfig.model_construct(
        name="x", cron="not a cron", enabled=True, timezone=None
    )
    assert compute_next_runs(bad_cron) == []
    bad_tz = ScheduleConfig.model_construct(
        name="x", cron="0 7 * * *", enabled=True, timezone="Nope/Zone"
    )
    assert compute_next_runs(bad_tz) == []


@pytest.mark.unit
def test_query_scheduler_api_none_when_not_this_project(tmp_path: Path) -> None:
    from dango.cli.commands.schedule import _query_scheduler_api

    with (
        patch("dango.cli.helpers.process_manager.is_project_server_running", return_value=False),
        patch("httpx.get") as mock_get,
    ):
        assert _query_scheduler_api(tmp_path) is None
    mock_get.assert_not_called()


@pytest.mark.unit
def test_query_scheduler_api_rejects_other_project_root(tmp_path: Path) -> None:
    from dango.cli.commands.schedule import _query_scheduler_api

    project = _project(tmp_path, [])
    loader = MagicMock()
    loader.return_value.load_config.return_value.platform.port = 8800
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"running": True, "project_root": "/somewhere/else", "schedules": []}
    with (
        patch("dango.cli.helpers.process_manager.is_project_server_running", return_value=True),
        patch("httpx.get", return_value=resp),
        patch("dango.config.loader.ConfigLoader", loader),
    ):
        assert _query_scheduler_api(project) is None

    resp.json.return_value = {
        "running": True,
        "project_root": str(project.resolve()),
        "schedules": [],
    }
    with (
        patch("dango.cli.helpers.process_manager.is_project_server_running", return_value=True),
        patch("httpx.get", return_value=resp),
        patch("dango.config.loader.ConfigLoader", loader),
    ):
        assert _query_scheduler_api(project) is not None


@pytest.mark.unit
def test_reload_skips_when_server_not_this_project(tmp_path: Path) -> None:
    from dango.cli.commands.schedule import _try_reload_running_scheduler

    project = _project(tmp_path, [])
    with (
        patch("dango.cli.helpers.process_manager.is_project_server_running", return_value=False),
        patch("dango.cli.helpers.port_manager.check_port_in_use", return_value=True),
        patch("requests.post") as mock_post,
        patch("dango.cli.commands.schedule.console") as mock_console,
    ):
        _try_reload_running_scheduler(project)
    mock_post.assert_not_called()
    printed = " ".join(str(c.args[0]) for c in mock_console.print.call_args_list)
    assert "next `dango start`" in printed


_DAILY = {
    "name": "daily",
    "type": "sync",
    "cron": "0 7 * * *",
    "sources": ["a"],
    "timezone": "America/New_York",
}


@pytest.mark.unit
@patch("dango.cli.utils.find_project_root")
def test_schedule_status_shows_loaded_and_live_next_run(
    mock_root: MagicMock, tmp_path: Path
) -> None:
    project = _project(tmp_path, [_DAILY])
    mock_root.return_value = project
    api = {
        "running": True,
        "job_count": 1,
        "project_root": str(project.resolve()),
        "schedules": [
            {
                "name": "daily",
                "enabled": True,
                "loaded": True,
                "next_run_time": "2030-01-02T07:00:00-05:00",
            }
        ],
    }
    with patch("dango.cli.commands.schedule._query_scheduler_api", return_value=api):
        result = CliRunner().invoke(cli, ["schedule", "status", "daily"])
    out = _plain(result.output)
    assert "Loaded:   yes" in out
    assert "2030-01-02 07:00" in out

    with patch("dango.cli.commands.schedule._query_scheduler_api", return_value=api):
        result = CliRunner().invoke(cli, ["schedule", "status"])
    out = _plain(result.output)
    assert "Scheduler: running (1 schedule(s) enabled, 1 loaded)" in out


@pytest.mark.unit
@patch("dango.cli.utils.find_project_root")
def test_schedule_status_earliest_next_run_honours_timezone(
    mock_root: MagicMock, tmp_path: Path
) -> None:
    # Same wall-clock cron in two zones: the zone whose 07:00 comes first must win.
    # Asia/Tokyo 07:00 is always earlier in absolute time than America/New_York 07:00
    # for the next occurrence only if it fires sooner; assert via compute_next_runs.
    tokyo = {**_DAILY, "name": "tokyo", "timezone": "Asia/Tokyo"}
    ny = {**_DAILY, "name": "ny"}
    project = _project(tmp_path, [ny, tokyo])
    mock_root.return_value = project
    expected = min((compute_next_runs(ScheduleConfig(**s))[0], s["name"]) for s in (ny, tokyo))
    with patch("dango.cli.commands.schedule._query_scheduler_api", return_value=None):
        result = CliRunner().invoke(cli, ["schedule", "status"])
    out = _plain(result.output)
    assert f"Next run: {expected[1]} at {expected[0].strftime('%Y-%m-%d %H:%M %Z')}" in out


@pytest.mark.unit
@patch("dango.cli.utils.find_project_root")
def test_schedule_status_not_found_aborts(mock_root: MagicMock, tmp_path: Path) -> None:
    mock_root.return_value = _project(tmp_path, [_DAILY])
    result = CliRunner().invoke(cli, ["schedule", "status", "missing"])
    assert result.exit_code == 1
    assert "not found" in _plain(result.output)
