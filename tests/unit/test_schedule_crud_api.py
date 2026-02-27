"""tests/unit/test_schedule_crud_api.py

Tests for the schedule CRUD API endpoints in dango/web/routes/schedules.py.
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


def _write_sources_yml(tmp_path: Path, sources: list[dict[str, Any]]) -> None:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir(parents=True, exist_ok=True)
    with open(dango_dir / "sources.yml", "w") as f:
        yaml.dump({"sources": sources}, f)


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
# Tests — Create schedule
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateSchedule:
    """POST /api/schedules."""

    def test_success(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)
        _write_sources_yml(tmp_path, [{"name": "stripe", "type": "stripe"}])
        scheduler = _mock_scheduler()
        app = _make_crud_app(tmp_path, scheduler=scheduler)

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(
                f"{_PATCH_ROOT}.load_sources_config",
                return_value=[{"name": "stripe", "type": "stripe"}],
            ),
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules",
                json=_sample_schedule(),
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "daily_sync"

        # Verify YAML was written
        yml_path = tmp_path / ".dango" / "schedules.yml"
        assert yml_path.exists()

    def test_validation_error(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)
        app = _make_crud_app(tmp_path, scheduler=_mock_scheduler())

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules",
                json={"name": "INVALID NAME", "cron": "bad", "sources": ["x"]},
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "DANGO-S004"

    def test_duplicate_name(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        _write_sources_yml(tmp_path, [{"name": "stripe", "type": "stripe"}])
        app = _make_crud_app(tmp_path, scheduler=_mock_scheduler())

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules",
                json=_sample_schedule(),
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 409
        assert resp.json()["error_code"] == "DANGO-S005"


# ---------------------------------------------------------------------------
# Tests — Update schedule
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUpdateSchedule:
    """PUT /api/schedules/{name}."""

    def test_success(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        _write_sources_yml(tmp_path, [{"name": "stripe", "type": "stripe"}])
        scheduler = _mock_scheduler()
        app = _make_crud_app(tmp_path, scheduler=scheduler)

        updated = _sample_schedule()
        updated["cron"] = "0 12 * * *"

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(
                f"{_PATCH_ROOT}.load_sources_config",
                return_value=[{"name": "stripe", "type": "stripe"}],
            ),
        ):
            client = TestClient(app)
            resp = client.put(
                "/api/schedules/daily_sync",
                json=updated,
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 200
        assert resp.json()["cron"] == "0 12 * * *"

    def test_not_found(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        app = _make_crud_app(tmp_path, scheduler=_mock_scheduler())

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.put(
                "/api/schedules/nonexistent",
                json=_sample_schedule(),
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "DANGO-S003"


# ---------------------------------------------------------------------------
# Tests — Delete schedule
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeleteSchedule:
    """DELETE /api/schedules/{name}."""

    def test_success(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        scheduler = _mock_scheduler()
        app = _make_crud_app(tmp_path, scheduler=scheduler)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.request(
                "DELETE",
                "/api/schedules/daily_sync",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # Verify YAML was updated
        with open(tmp_path / ".dango" / "schedules.yml") as f:
            data: dict[str, Any] = yaml.safe_load(f)
        assert len(data["schedules"]) == 0

    def test_not_found(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        app = _make_crud_app(tmp_path, scheduler=_mock_scheduler())

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.request(
                "DELETE",
                "/api/schedules/nonexistent",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 404


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
# Tests — Audit logging verification
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuditLogging:
    """Verify audit events are logged on write endpoints."""

    def test_create_logs_audit_event(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)
        scheduler = _mock_scheduler()
        app = _make_crud_app(tmp_path, scheduler=scheduler)

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(
                f"{_PATCH_ROOT}.load_sources_config",
                return_value=[{"name": "stripe", "type": "stripe"}],
            ),
            patch(f"{_PATCH_ROOT}.log_auth_event") as mock_audit,
        ):
            client = TestClient(app)
            client.post(
                "/api/schedules",
                json=_sample_schedule(),
                headers={"X-Requested-With": "fetch"},
            )

        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        from dango.auth.audit import AuditEvent

        assert call_args[0][0] == AuditEvent.SCHEDULE_CREATED

    def test_delete_logs_audit_event(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        scheduler = _mock_scheduler()
        app = _make_crud_app(tmp_path, scheduler=scheduler)

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(f"{_PATCH_ROOT}.log_auth_event") as mock_audit,
        ):
            client = TestClient(app)
            client.request(
                "DELETE",
                "/api/schedules/daily_sync",
                headers={"X-Requested-With": "fetch"},
            )

        mock_audit.assert_called_once()
        from dango.auth.audit import AuditEvent

        assert mock_audit.call_args[0][0] == AuditEvent.SCHEDULE_DELETED


# ---------------------------------------------------------------------------
# Tests — Scheduler reload verification
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSchedulerReloadIntegration:
    """Verify scheduler is reloaded after config changes."""

    def test_create_calls_reload(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)
        scheduler = _mock_scheduler()
        app = _make_crud_app(tmp_path, scheduler=scheduler)

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(
                f"{_PATCH_ROOT}.load_sources_config",
                return_value=[{"name": "stripe", "type": "stripe"}],
            ),
            patch(f"{_PATCH_ROOT}.reload_schedules") as mock_reload,
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules",
                json=_sample_schedule(),
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 201
        mock_reload.assert_called_once()

    def test_update_calls_reload(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        scheduler = _mock_scheduler()
        app = _make_crud_app(tmp_path, scheduler=scheduler)

        updated = _sample_schedule()
        updated["cron"] = "0 12 * * *"

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(
                f"{_PATCH_ROOT}.load_sources_config",
                return_value=[{"name": "stripe", "type": "stripe"}],
            ),
            patch(f"{_PATCH_ROOT}.reload_schedules") as mock_reload,
        ):
            client = TestClient(app)
            resp = client.put(
                "/api/schedules/daily_sync",
                json=updated,
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 200
        mock_reload.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — Rename guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRenameGuard:
    """Verify update rejects name changes."""

    def test_rename_rejected(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _write_schedules_yml(tmp_path, [_sample_schedule()])
        app = _make_crud_app(tmp_path, scheduler=_mock_scheduler())

        renamed = _sample_schedule()
        renamed["name"] = "different_name"

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.put(
                "/api/schedules/daily_sync",
                json=renamed,
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 400
        assert "does not match" in resp.json()["message"]


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

    def test_viewer_cannot_create_schedule(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)
        app = _make_crud_app(tmp_path, user=_make_viewer_user(), scheduler=_mock_scheduler())

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules",
                json=_sample_schedule(),
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 403

    def test_editor_cannot_create_schedule(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)
        app = _make_crud_app(tmp_path, user=_make_editor_user(), scheduler=_mock_scheduler())

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules",
                json=_sample_schedule(),
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 403

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
