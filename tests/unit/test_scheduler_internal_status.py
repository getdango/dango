"""tests/unit/test_scheduler_internal_status.py

Tests for GET /api/internal/scheduler/status: localhost-only guard, per-schedule
loaded/next-run reporting, 503 without a scheduler, and public auth registration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.config.schedules import get_schedule_job_id

_URL = "/api/internal/scheduler/status"
_ROOT_PATCH = "dango.web.routes.schedules.get_project_root"


def _write_schedules(tmp_path: Path) -> None:
    d = tmp_path / ".dango"
    d.mkdir(parents=True, exist_ok=True)
    data = {
        "schedules": [
            {"name": "daily", "type": "sync", "cron": "0 7 * * *", "sources": ["a"]},
            {
                "name": "off",
                "type": "sync",
                "cron": "0 8 * * *",
                "sources": ["a"],
                "enabled": False,
            },
        ]
    }
    (d / "schedules.yml").write_text(yaml.safe_dump(data))


def _fake_scheduler(running: bool = True) -> MagicMock:
    job = MagicMock()
    job.id = get_schedule_job_id("daily")
    job.next_run_time = datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)
    sched = MagicMock()
    sched.get_status.return_value = {"running": running, "job_count": 1}
    sched.get_jobs.return_value = [job]
    return sched


def _client(scheduler: Any, host: str = "127.0.0.1") -> TestClient:
    from dango.web.routes.schedules import router

    app = FastAPI()
    if scheduler is not None:
        app.state.scheduler = scheduler
    app.include_router(router)
    return TestClient(app, client=(host, 50000))


@pytest.mark.unit
def test_status_localhost_ok(tmp_path: Path) -> None:
    _write_schedules(tmp_path)
    with patch(_ROOT_PATCH, return_value=tmp_path):
        resp = _client(_fake_scheduler()).get(_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is True
    assert body["job_count"] == 1
    assert body["project_root"] == str(tmp_path.resolve())
    by_name = {s["name"]: s for s in body["schedules"]}
    assert by_name["daily"]["loaded"] is True
    assert by_name["daily"]["next_run_time"] == "2026-10-02T07:00:00+00:00"
    assert by_name["off"]["loaded"] is False
    assert by_name["off"]["enabled"] is False
    assert by_name["off"]["next_run_time"] is None


@pytest.mark.unit
def test_status_not_running_reports_nothing_loaded(tmp_path: Path) -> None:
    _write_schedules(tmp_path)
    with patch(_ROOT_PATCH, return_value=tmp_path):
        body = _client(_fake_scheduler(running=False)).get(_URL).json()
    assert body["running"] is False
    assert all(s["loaded"] is False for s in body["schedules"])


@pytest.mark.unit
def test_status_rejects_forwarded_for(tmp_path: Path) -> None:
    _write_schedules(tmp_path)
    with patch(_ROOT_PATCH, return_value=tmp_path):
        resp = _client(_fake_scheduler()).get(_URL, headers={"X-Forwarded-For": "1.2.3.4"})
    assert resp.status_code == 403


@pytest.mark.unit
def test_status_rejects_non_localhost(tmp_path: Path) -> None:
    _write_schedules(tmp_path)
    with patch(_ROOT_PATCH, return_value=tmp_path):
        resp = _client(_fake_scheduler(), host="10.0.0.5").get(_URL)
    assert resp.status_code == 403


@pytest.mark.unit
def test_status_no_scheduler_503(tmp_path: Path) -> None:
    with patch(_ROOT_PATCH, return_value=tmp_path):
        resp = _client(None).get(_URL)
    assert resp.status_code == 503
    assert resp.json() == {"message": "Scheduler not available"}


@pytest.mark.unit
def test_status_route_is_public_in_auth_middleware() -> None:
    from dango.web.middleware.auth import _PUBLIC_EXACT

    assert _URL in _PUBLIC_EXACT
