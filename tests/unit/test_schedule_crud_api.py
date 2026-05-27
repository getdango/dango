"""tests/unit/test_schedule_crud_api.py

Tests for the schedule read-only API endpoints in dango/web/routes/schedules.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from dango.auth.models import Role, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_ROOT = "dango.web.routes.schedules"


def _make_admin_user() -> User:
    return User(id="admin-id", email="admin@test.com", role=Role.ADMIN, is_active=True)


def _make_editor_user() -> User:
    return User(id="editor-id", email="editor@test.com", role=Role.EDITOR, is_active=True)


def _make_viewer_user() -> User:
    return User(id="viewer-id", email="viewer@test.com", role=Role.VIEWER, is_active=True)


def _make_crud_app(
    tmp_path: Path,
    user: User | None = None,
    scheduler: Any = None,
) -> Any:
    """Build a minimal FastAPI app with schedules router and injected user."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from dango.exceptions import AuthorizationError
    from dango.web.routes.schedules import router

    app = FastAPI()
    app.state.project_root = tmp_path
    if scheduler is not None:
        app.state.scheduler = scheduler
    app.include_router(router)

    test_user = user or _make_admin_user()

    @app.middleware("http")
    async def inject_user(request: Any, call_next: Any) -> Any:
        request.state.user = test_user
        return await call_next(request)

    @app.exception_handler(AuthorizationError)
    async def auth_error_handler(request: Any, exc: AuthorizationError) -> Any:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    return app


def _write_schedules_yml(tmp_path: Path, schedules: list[dict[str, Any]]) -> None:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir(parents=True, exist_ok=True)
    with open(dango_dir / "schedules.yml", "w") as f:
        yaml.dump({"schedules": schedules}, f)


def _sample_schedule() -> dict[str, Any]:
    return {
        "name": "daily_sync",
        "type": "sync",
        "cron": "0 6 * * *",
        "sources": ["stripe"],
        "enabled": True,
    }


def _mock_scheduler() -> MagicMock:
    scheduler = MagicMock()
    scheduler.get_jobs.return_value = []
    scheduler.cancel_job.return_value = True
    mock_job = MagicMock()
    mock_job.id = "mock-job-id"
    scheduler.add_job.return_value = mock_job
    return scheduler


# ---------------------------------------------------------------------------
# Tests — List schedules
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListSchedules:
    """GET /api/schedules."""

    def test_list_with_data(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        app = _make_crud_app(tmp_path, scheduler=_mock_scheduler())

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "daily_sync"

    def test_list_empty(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        app = _make_crud_app(tmp_path)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules")

        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Tests — Get schedule detail
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetSchedule:
    """GET /api/schedules/{name}."""

    def test_success(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        app = _make_crud_app(tmp_path, scheduler=_mock_scheduler())

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules/daily_sync")

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "daily_sync"
        assert "recent_history" in data

    def test_not_found(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        app = _make_crud_app(tmp_path)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules/nonexistent")

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "DANGO-S003"


# ---------------------------------------------------------------------------
# Tests — Trigger schedule
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTriggerSchedule:
    """POST /api/schedules/{name}/trigger."""

    def test_success(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        scheduler = _mock_scheduler()
        mock_job = MagicMock()
        mock_job.id = "manual:daily_sync:2026-01-01"
        scheduler.add_job.return_value = mock_job
        app = _make_crud_app(tmp_path, scheduler=scheduler)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules/daily_sync/trigger",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "triggered"
        scheduler.add_job.assert_called_once()

    def test_not_found(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        scheduler = _mock_scheduler()
        app = _make_crud_app(tmp_path, scheduler=scheduler)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules/nonexistent/trigger",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 404

    def test_scheduler_unavailable(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        # No scheduler on app.state
        app = _make_crud_app(tmp_path)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules/daily_sync/trigger",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 503
        assert resp.json()["error_code"] == "DANGO-S006"


# ---------------------------------------------------------------------------
# Tests — Reload schedules
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReloadSchedules:
    """POST /api/schedules/reload."""

    def test_success(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from dango.config.schedules import ReloadResult

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        scheduler = _mock_scheduler()
        app = _make_crud_app(tmp_path, scheduler=scheduler)

        mock_result = ReloadResult(added=["daily_sync"], removed=[], updated=[])

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(f"{_PATCH_ROOT}.reload_schedules", return_value=mock_result),
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules/reload",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "daily_sync" in data["added"]


# ---------------------------------------------------------------------------
# Tests — Cancel job
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCancelJob:
    """POST /api/schedules/jobs/{job_id}/cancel."""

    def test_success(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        scheduler = _mock_scheduler()
        scheduler.cancel_job.return_value = True
        app = _make_crud_app(tmp_path, scheduler=scheduler)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules/jobs/schedule:daily_sync/cancel",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_not_found(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        scheduler = _mock_scheduler()
        scheduler.cancel_job.return_value = False
        app = _make_crud_app(tmp_path, scheduler=scheduler)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules/jobs/nonexistent/cancel",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "DANGO-S007"


# ---------------------------------------------------------------------------
# Tests — Unscheduled sources
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUnscheduledSources:
    """GET /api/sources/unscheduled."""

    def test_returns_unscheduled(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        app = _make_crud_app(tmp_path)

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(
                f"{_PATCH_ROOT}.load_sources_config",
                return_value=[
                    {"name": "stripe", "type": "stripe"},
                    {"name": "hubspot", "type": "hubspot"},
                ],
            ),
        ):
            client = TestClient(app)
            resp = client.get("/api/sources/unscheduled")

        assert resp.status_code == 200
        data = resp.json()
        assert "hubspot" in data["sources"]
        assert "stripe" not in data["sources"]


# ---------------------------------------------------------------------------
# Tests — RBAC enforcement
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRBACEnforcement:
    """Verify permission checks on schedule endpoints."""

    def test_viewer_can_list_schedules(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        app = _make_crud_app(tmp_path, user=_make_viewer_user(), scheduler=_mock_scheduler())

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules")

        assert resp.status_code == 200

    def test_editor_can_trigger_schedule(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        scheduler = _mock_scheduler()
        app = _make_crud_app(tmp_path, user=_make_editor_user(), scheduler=scheduler)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules/daily_sync/trigger",
                json={},
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 200
