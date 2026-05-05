"""tests/unit/test_schedule_readonly_guard.py

Verify that config-mutating schedule/webhook endpoints have been removed
and return appropriate error codes (405 or 404).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from dango.auth.models import Role, User

_PATCH_ROOT = "dango.web.routes.schedules"


def _make_admin_user() -> User:
    return User(id="admin-id", email="admin@test.com", role=Role.ADMIN, is_active=True)


def _make_app(tmp_path: Path) -> Any:
    """Build a minimal FastAPI app with schedules router."""
    from fastapi import FastAPI

    from dango.web.routes.schedules import router

    app = FastAPI()
    app.state.project_root = tmp_path

    scheduler = MagicMock()
    scheduler.get_jobs.return_value = []
    scheduler.cancel_job.return_value = True
    mock_job = MagicMock()
    mock_job.id = "mock-job-id"
    scheduler.add_job.return_value = mock_job
    app.state.scheduler = scheduler
    app.include_router(router)

    user = _make_admin_user()

    @app.middleware("http")
    async def inject_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        return await call_next(request)

    return app


def _write_schedules_yml(tmp_path: Path) -> None:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir(parents=True, exist_ok=True)
    schedule = {
        "name": "daily_sync",
        "type": "sync",
        "cron": "0 6 * * *",
        "sources": ["stripe"],
        "enabled": True,
    }
    with open(dango_dir / "schedules.yml", "w") as f:
        yaml.dump({"schedules": [schedule]}, f)


# ---------------------------------------------------------------------------
# Removed write endpoints must return 405 or 404
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemovedEndpointsReturn405Or404:
    """Config-mutating endpoints have been removed (BUG-175)."""

    def test_post_schedules_returns_405(self, tmp_path: Path) -> None:
        from starlette.testclient import TestClient

        _write_schedules_yml(tmp_path)
        app = _make_app(tmp_path)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post("/api/schedules", json={"name": "x"})

        assert resp.status_code == 405

    def test_put_schedule_returns_405(self, tmp_path: Path) -> None:
        from starlette.testclient import TestClient

        _write_schedules_yml(tmp_path)
        app = _make_app(tmp_path)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.put("/api/schedules/daily_sync", json={"name": "x"})

        assert resp.status_code == 405

    def test_delete_schedule_returns_405(self, tmp_path: Path) -> None:
        from starlette.testclient import TestClient

        _write_schedules_yml(tmp_path)
        app = _make_app(tmp_path)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.delete("/api/schedules/daily_sync")

        assert resp.status_code == 405

    def test_post_webhook_returns_404_or_405(self, tmp_path: Path) -> None:
        from starlette.testclient import TestClient

        app = _make_app(tmp_path)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/notifications/webhooks",
                json={"name": "x", "url": "https://example.com", "format": "generic"},
            )

        assert resp.status_code in (404, 405)

    def test_delete_webhook_returns_404_or_405(self, tmp_path: Path) -> None:
        from starlette.testclient import TestClient

        app = _make_app(tmp_path)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.delete("/api/notifications/webhooks/test_hook")

        assert resp.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Kept endpoints still work
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKeptEndpointsStillWork:
    """Read-only and operational endpoints are unaffected."""

    def test_get_schedules_returns_200(self, tmp_path: Path) -> None:
        from starlette.testclient import TestClient

        _write_schedules_yml(tmp_path)
        app = _make_app(tmp_path)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules")

        assert resp.status_code == 200

    def test_trigger_still_works(self, tmp_path: Path) -> None:
        from starlette.testclient import TestClient

        _write_schedules_yml(tmp_path)
        app = _make_app(tmp_path)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post("/api/schedules/daily_sync/trigger")

        assert resp.status_code == 200

    def test_notification_test_still_works(self, tmp_path: Path) -> None:
        from starlette.testclient import TestClient

        app = _make_app(tmp_path)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post("/api/notifications/test")

        # 400 expected because no webhooks configured — but the route exists
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "DANGO-S008"
