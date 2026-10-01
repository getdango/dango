"""tests/unit/test_mcp_schedules_live.py

MCP schedule tools wired to the live scheduler status (M7c): loaded / live_next_run,
shared compute_next_runs, and `loaded` reported after activation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from dango.cli.commands import mcp_schedules


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    from dango.config.helpers import save_config

    save_config(sample_config, tmp_path)
    monkeypatch.setattr(mcp_schedules, "_get_project_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_schedules, "_server_running", lambda root: False)
    return tmp_path


def _api(project: Path, **entries: dict[str, Any]) -> dict[str, Any]:
    return {
        "running": True,
        "job_count": len(entries),
        "project_root": str(project),
        "schedules": [{"name": n, **e} for n, e in entries.items()],
    }


def _patch_api(monkeypatch: pytest.MonkeyPatch, value: dict[str, Any] | None) -> None:
    monkeypatch.setattr("dango.cli.commands.schedule._query_scheduler_api", lambda root: value)


def test_list_schedules_reports_loaded_and_live_next_run(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_schedules.add_schedule("loaded_one", "0 7 * * *", ["test_source"])
    mcp_schedules.add_schedule("not_loaded", "0 8 * * *", ["test_source"])
    monkeypatch.setattr(mcp_schedules, "_server_running", lambda root: True)
    _patch_api(
        monkeypatch,
        _api(
            project,
            loaded_one={"enabled": True, "loaded": True, "next_run_time": "2030-01-01T07:00:00"},
            not_loaded={"enabled": True, "loaded": False, "next_run_time": None},
        ),
    )
    result = mcp_schedules.list_schedules()
    by_name = {s["name"]: s for s in result["schedules"]}
    assert result["server_running"] is True
    assert result["scheduler"] == {"running": True, "job_count": 2}
    assert by_name["loaded_one"]["loaded"] is True
    assert by_name["loaded_one"]["live_next_run"] == "2030-01-01T07:00:00"
    assert by_name["not_loaded"]["loaded"] is False
    assert by_name["not_loaded"]["live_next_run"] is None
    assert by_name["not_loaded"]["next_run"]  # computed independently of the live scheduler


def test_list_schedules_scheduler_none_when_other_project(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_schedules.add_schedule("daily", "0 7 * * *", ["test_source"])
    monkeypatch.setattr(mcp_schedules, "_server_running", lambda root: True)
    _patch_api(monkeypatch, None)
    result = mcp_schedules.list_schedules()
    assert result["scheduler"] is None
    assert result["schedules"][0]["loaded"] is None
    assert result["schedules"][0]["live_next_run"] is None


def test_next_run_uses_compute_next_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    when = datetime(2031, 5, 6, 7, 0, tzinfo=timezone.utc)
    seen: list[tuple[Any, int]] = []

    def _fake(sched: Any, count: int = 1) -> list[datetime]:
        seen.append((sched, count))
        return [when]

    monkeypatch.setattr("dango.config.schedules.compute_next_runs", _fake)
    sentinel = object()
    assert mcp_schedules._next_run_iso(sentinel) == when.isoformat()
    assert seen == [(sentinel, 1)]
    monkeypatch.setattr("dango.config.schedules.compute_next_runs", lambda s, c=1: [])
    assert mcp_schedules._next_run_iso(sentinel) is None


def test_activate_reports_loaded_for_schedule(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import requests

    class _Resp:
        status_code = 200

    monkeypatch.setattr(mcp_schedules, "_server_running", lambda root: True)
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp())
    _patch_api(
        monkeypatch,
        _api(project, mine={"enabled": True, "loaded": True, "next_run_time": "t"}),
    )
    result = mcp_schedules.add_schedule("mine", "0 7 * * *", ["test_source"])
    assert result["activation"] == "reloaded"
    assert result["loaded"] is True

    # Absent from the live scheduler -> False; status unreadable -> None.
    assert mcp_schedules._activate(project, "ghost")["loaded"] is False
    _patch_api(monkeypatch, None)
    assert mcp_schedules._activate(project, "mine")["loaded"] is None
    # No name -> no loaded key (remove/reload keep their shape).
    assert "loaded" not in mcp_schedules._activate(project)
