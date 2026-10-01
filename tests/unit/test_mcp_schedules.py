"""tests/unit/test_mcp_schedules.py

Tests for the MCP schedule tools (dango/cli/commands/mcp_schedules.py):
list/add/update/enable/remove/reload, activation reporting, and the
webhook-preservation regression (MCP add_schedule used to delete notifications).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml

from dango.cli.commands import mcp_schedules
from dango.config.schedules import load_schedules_config

_REAL_SERVER_RUNNING = mcp_schedules._server_running
_NOTIF = {"webhooks": [{"name": "team_slack", "url": "https://hooks.example.com/x"}]}


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Real Dango project on disk (one source 'test_source') wired to the tools."""
    from dango.config.helpers import save_config

    save_config(sample_config, tmp_path)
    monkeypatch.setattr(mcp_schedules, "_get_project_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_schedules, "_server_running", lambda project_root: False)


def _yml(project: Path) -> Path:
    return project / ".dango" / "schedules.yml"


def _add(name: str = "daily_sync", cron: str = "0 7 * * *", **kw: Any) -> dict[str, Any]:
    return mcp_schedules.add_schedule(name, cron, kw.pop("sources", ["test_source"]), **kw)


@pytest.mark.unit
class TestAddSchedule:
    def test_add_schedule_missing_source(self, project: Path) -> None:
        result = mcp_schedules.add_schedule("daily_sync", "0 7 * * *", ["nonexistent_source"])
        assert "nonexistent_source" in result["error"]
        assert "add_source()" in result["error"]
        assert not _yml(project).exists()

    def test_add_schedule_success_persists_to_disk(self, project: Path) -> None:
        result = _add(timezone="Asia/Singapore")
        assert result["status"] == "created"
        assert result["schedule_name"] == "daily_sync"
        assert result["activation"] == "server_not_running"
        assert result["schedule"]["next_run"]
        datetime.fromisoformat(result["schedule"]["next_run"])
        assert {s.name for s in load_schedules_config(project).schedules} == {"daily_sync"}

    def test_add_schedule_duplicate_name(self, project: Path) -> None:
        _add()
        result = _add(cron="0 8 * * *")
        assert result == {"error": "Schedule 'daily_sync' already exists"}

    def test_add_schedule_invalid_cron_returns_error(self, project: Path) -> None:
        result = _add(cron="not a cron")
        assert "Invalid cron expression" in result["error"]
        assert "pydantic.dev" not in result["error"]
        assert "\n" not in result["error"]
        assert not _yml(project).exists()

    def test_add_schedule_unknown_timezone_returns_error(self, project: Path) -> None:
        result = _add(timezone="Mars/Olympus")
        assert result == {"error": "Unknown timezone: 'Mars/Olympus'"}
        assert not _yml(project).exists()

    def test_add_schedule_git_warning_present(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_schedules, "_git_warnings", lambda root: ["On branch 'master'"])
        result = _add("weekly_sync", "0 7 * * 1")
        assert result["status"] == "created"
        assert result["git_warning"] == ["On branch 'master'"]

    def test_add_schedule_git_warning_absent(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_schedules, "_git_warnings", lambda root: [])
        result = _add("monthly_sync", "0 7 1 * *")
        assert result["status"] == "created"
        assert "git_warning" not in result

    def test_add_schedule_preserves_webhook_notifications(self, project: Path) -> None:
        """Data-loss regression: add_schedule used to rewrite the file with only `schedules`."""
        _yml(project).parent.mkdir(exist_ok=True)
        _yml(project).write_text(yaml.dump({"schedules": [], "notifications": _NOTIF}))
        result = _add()
        assert result["status"] == "created"
        raw = yaml.safe_load(_yml(project).read_text())
        assert raw["notifications"] == _NOTIF
        assert [s["name"] for s in raw["schedules"]] == ["daily_sync"]


@pytest.mark.unit
class TestListSchedules:
    def test_list_schedules_reports_next_run_and_enabled(self, project: Path) -> None:
        _add("on_sched")
        _add("off_sched")
        mcp_schedules.set_schedule_enabled("off_sched", False)
        result = mcp_schedules.list_schedules()
        by_name = {s["name"]: s for s in result["schedules"]}
        assert by_name["off_sched"]["next_run"] is None
        assert by_name["off_sched"]["enabled"] is False
        datetime.fromisoformat(by_name["on_sched"]["next_run"])
        assert result["server_running"] is False
        assert result["scheduler"] is None

    def test_next_run_respects_timezone(self, project: Path) -> None:
        _add(cron="0 7 * * *", timezone="Asia/Singapore")
        nxt = datetime.fromisoformat(mcp_schedules.list_schedules()["schedules"][0]["next_run"])
        assert nxt.utcoffset() == timedelta(hours=8)
        assert nxt.hour == 7


@pytest.mark.unit
class TestUpdateAndToggle:
    def test_update_schedule_revalidates(self, project: Path) -> None:
        _add()
        before = _yml(project).read_text()
        result = mcp_schedules.update_schedule("daily_sync", cron="bad")
        assert "error" in result
        assert _yml(project).read_text() == before

    def test_update_schedule_changes_cron_and_keeps_others(self, project: Path) -> None:
        _add("first", timezone="Asia/Singapore")
        _add("second")
        result = mcp_schedules.update_schedule("first", cron="0 9 * * *")
        assert result["status"] == "updated"
        scheds = load_schedules_config(project).schedules
        assert [s.name for s in scheds] == ["first", "second"]
        assert scheds[0].cron == "0 9 * * *"
        assert scheds[0].timezone == "Asia/Singapore"
        assert scheds[0].sources == ["test_source"]

    def test_update_schedule_skip_dbt_toggles_type(self, project: Path) -> None:
        _add()
        result = mcp_schedules.update_schedule("daily_sync", skip_dbt=True)
        assert result["schedule"]["type"] == "sync_only"

    def test_update_schedule_errors(self, project: Path) -> None:
        _add()
        assert mcp_schedules.update_schedule("nope", cron="0 1 * * *") == {
            "error": "Schedule 'nope' not found"
        }
        assert mcp_schedules.update_schedule("daily_sync") == {"error": "Nothing to update"}

    def test_set_schedule_enabled_toggles_and_unchanged(self, project: Path) -> None:
        _add()
        result = mcp_schedules.set_schedule_enabled("daily_sync", False)
        assert result["status"] == "disabled"
        assert result["schedule"]["next_run"] is None
        mtime = os.stat(_yml(project)).st_mtime_ns
        again = mcp_schedules.set_schedule_enabled("daily_sync", False)
        assert again["status"] == "unchanged"
        assert again["activation"] == "server_not_running"
        assert os.stat(_yml(project)).st_mtime_ns == mtime
        assert mcp_schedules.set_schedule_enabled("daily_sync", True)["status"] == "enabled"

    def test_remove_schedule(self, project: Path) -> None:
        _add()
        result = mcp_schedules.remove_schedule("daily_sync")
        assert result["status"] == "removed"
        assert load_schedules_config(project).schedules == []
        assert "error" in mcp_schedules.remove_schedule("daily_sync")

    def _with_broken_schedule(self, project: Path) -> None:
        """Two schedules; 'broken' references a source that no longer exists."""
        _add("healthy")
        raw = yaml.safe_load(_yml(project).read_text())
        raw["schedules"].append(
            {"name": "broken", "type": "sync", "cron": "0 1 * * *", "sources": ["deleted_src"]}
        )
        _yml(project).write_text(yaml.dump(raw))

    def test_disable_works_when_another_schedule_is_broken(self, project: Path) -> None:
        self._with_broken_schedule(project)
        assert mcp_schedules.set_schedule_enabled("healthy", False)["status"] == "disabled"
        assert mcp_schedules.set_schedule_enabled("broken", False)["status"] == "disabled"
        assert mcp_schedules.remove_schedule("broken")["status"] == "removed"

    def test_unrelated_errors_become_warnings(self, project: Path) -> None:
        self._with_broken_schedule(project)
        result = mcp_schedules.update_schedule("healthy", cron="0 5 * * *")
        assert result["status"] == "updated"
        assert any("deleted_src" in w for w in result["warnings"])

    def test_source_named_like_schedule_does_not_misattribute_error(self, project: Path) -> None:
        """A broken schedule referencing a source named 'healthy' must not block 'healthy'."""
        _add("healthy")
        raw = yaml.safe_load(_yml(project).read_text())
        raw["schedules"].append(
            {"name": "other", "type": "sync", "cron": "0 1 * * *", "sources": ["healthy"]}
        )
        _yml(project).write_text(yaml.dump(raw))
        result = mcp_schedules.update_schedule("healthy", cron="0 5 * * *")
        assert result["status"] == "updated"
        assert result["warnings"]

    def test_enabling_broken_schedule_is_rejected(self, project: Path) -> None:
        self._with_broken_schedule(project)
        mcp_schedules.set_schedule_enabled("broken", False)
        result = mcp_schedules.set_schedule_enabled("broken", True)
        assert "deleted_src" in result["error"]


@pytest.mark.unit
class TestActivation:
    def test_activation_reloaded_when_server_running(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_schedules, "_server_running", lambda root: True)
        calls: list[str] = []

        class _Resp:
            status_code = 200

        def _post(url: str, **kw: Any) -> _Resp:
            calls.append(url)
            return _Resp()

        monkeypatch.setattr(requests, "post", _post)
        from dango.config import ConfigLoader

        port = ConfigLoader(project).load_config().platform.port
        result = _add()
        assert result["activation"] == "reloaded"
        assert calls == [f"http://localhost:{port}/api/internal/schedules/reload"]

    def test_activation_reload_failed_on_http_error(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_schedules, "_server_running", lambda root: True)

        class _Resp:
            status_code = 503

        monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp())
        assert _add()["activation"] == "reload_failed"

    def test_activation_never_raises_on_connection_error(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_schedules, "_server_running", lambda root: True)

        def _boom(url: str, **kw: Any) -> None:
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(requests, "post", _boom)
        assert _add()["activation"] == "reload_failed"

    def test_reload_schedules_tool(self, project: Path) -> None:
        result = mcp_schedules.reload_schedules()
        assert result["status"] == "ok"
        assert result["activation"] == "server_not_running"

    def test_server_running_false_when_pid_file_absent(self, tmp_path: Path) -> None:
        # The autouse fixture patches the module attribute; use the saved original.
        assert _REAL_SERVER_RUNNING(tmp_path) is False


@pytest.mark.unit
def test_add_schedule_still_registered_as_mcp_tool() -> None:
    from dango.cli.commands import mcp_server

    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    expected = {
        "add_schedule",
        "list_schedules",
        "update_schedule",
        "set_schedule_enabled",
        "remove_schedule",
        "reload_schedules",
    }
    assert expected <= names
