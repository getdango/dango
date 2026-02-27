"""tests/unit/test_sync_trigger_api.py

Tests for the sync trigger API endpoints (POST /api/sync/trigger,
GET /api/sync/status/{record_id}) added by TASK-040c.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dango.auth.models import Role, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_ROOT = "dango.web.routes.sync"


def _make_admin_user() -> User:
    return User(id="admin-id", email="admin@test.com", role=Role.ADMIN, is_active=True)


def _make_viewer_user() -> User:
    return User(id="viewer-id", email="viewer@test.com", role=Role.VIEWER, is_active=True)


def _make_app(tmp_path: Path, user: User | None = None) -> Any:
    """Build a minimal FastAPI app with the sync router and injected user."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from dango.exceptions import AuthorizationError
    from dango.web.routes.sync import router

    app = FastAPI()
    app.state.project_root = tmp_path

    test_user = user or _make_admin_user()

    @app.middleware("http")
    async def inject_user(request: Any, call_next: Any) -> Any:
        request.state.user = test_user
        return await call_next(request)

    @app.exception_handler(AuthorizationError)
    async def auth_error_handler(request: Any, exc: AuthorizationError) -> Any:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# POST /api/sync/trigger tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSyncTriggerEndpoint:
    """Tests for POST /api/sync/trigger."""

    @patch(f"{_PATCH_ROOT}._run_manual_sync")
    @patch(f"{_PATCH_ROOT}.log_auth_event")
    @patch(f"{_PATCH_ROOT}.record_start", return_value=42)
    @patch(f"{_PATCH_ROOT}.get_scheduler_db_path")
    @patch(f"{_PATCH_ROOT}.load_sources_config", return_value=[{"name": "my_source"}])
    @patch(f"{_PATCH_ROOT}.get_project_root", return_value=Path("/fake"))
    def test_trigger_returns_job_id(
        self, mock_root, mock_sources, mock_db_path, mock_start, mock_audit, mock_bg, tmp_path
    ):
        from starlette.testclient import TestClient

        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post(
            "/api/sync/trigger",
            json={"sources": ["my_source"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == 42
        assert data["sources"] == ["my_source"]
        assert data["status"] == "started"

    @patch(f"{_PATCH_ROOT}.load_sources_config", return_value=[{"name": "a"}])
    @patch(f"{_PATCH_ROOT}.get_project_root", return_value=Path("/fake"))
    def test_unknown_source_returns_404(self, mock_root, mock_sources, tmp_path):
        from starlette.testclient import TestClient

        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post(
            "/api/sync/trigger",
            json={"sources": ["nonexistent"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 404
        assert "DANGO-SYNC-001" in resp.json()["error_code"]

    @patch(f"{_PATCH_ROOT}.load_sources_config", return_value=[{"name": "s1"}])
    @patch(f"{_PATCH_ROOT}.get_project_root", return_value=Path("/fake"))
    def test_invalid_backfill_returns_422(self, mock_root, mock_sources, tmp_path):
        from starlette.testclient import TestClient

        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post(
            "/api/sync/trigger",
            json={"sources": ["s1"], "backfill": "abc"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 422
        assert "DANGO-SYNC-002" in resp.json()["error_code"]

    def test_viewer_gets_403(self, tmp_path):
        from starlette.testclient import TestClient

        app = _make_app(tmp_path, user=_make_viewer_user())
        client = TestClient(app)

        resp = client.post(
            "/api/sync/trigger",
            json={"sources": ["s1"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 403

    @patch(f"{_PATCH_ROOT}._run_manual_sync")
    @patch(f"{_PATCH_ROOT}.log_auth_event")
    @patch(f"{_PATCH_ROOT}.record_start", return_value=99)
    @patch(f"{_PATCH_ROOT}.get_scheduler_db_path")
    @patch(f"{_PATCH_ROOT}.load_sources_config", return_value=[{"name": "s1"}])
    @patch(f"{_PATCH_ROOT}.get_project_root", return_value=Path("/fake"))
    def test_audit_event_logged(
        self, mock_root, mock_sources, mock_db_path, mock_start, mock_audit, mock_bg, tmp_path
    ):
        from starlette.testclient import TestClient

        from dango.auth.audit import AuditEvent

        app = _make_app(tmp_path)
        client = TestClient(app)

        client.post(
            "/api/sync/trigger",
            json={"sources": ["s1"], "full_refresh": True},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[0][0] == AuditEvent.SYNC_TRIGGERED
        assert call_kwargs[1]["details"]["sources"] == ["s1"]
        assert call_kwargs[1]["details"]["full_refresh"] is True


# ---------------------------------------------------------------------------
# GET /api/sync/status/{record_id} tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSyncStatusEndpoint:
    """Tests for GET /api/sync/status/{record_id}."""

    @patch(
        f"{_PATCH_ROOT}.get_execution_record",
        return_value={"id": 1, "status": "running", "schedule_name": "manual"},
    )
    @patch(f"{_PATCH_ROOT}.get_scheduler_db_path")
    @patch(f"{_PATCH_ROOT}.get_project_root", return_value=Path("/fake"))
    def test_returns_record(self, mock_root, mock_db_path, mock_record, tmp_path):
        from starlette.testclient import TestClient

        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/sync/status/1")
        assert resp.status_code == 200
        assert resp.json()["id"] == 1
        assert resp.json()["status"] == "running"

    @patch(f"{_PATCH_ROOT}.get_execution_record", return_value=None)
    @patch(f"{_PATCH_ROOT}.get_scheduler_db_path")
    @patch(f"{_PATCH_ROOT}.get_project_root", return_value=Path("/fake"))
    def test_unknown_id_returns_404(self, mock_root, mock_db_path, mock_record, tmp_path):
        from starlette.testclient import TestClient

        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/sync/status/999")
        assert resp.status_code == 404
        assert "DANGO-SYNC-003" in resp.json()["error_code"]

    def test_viewer_gets_403(self, tmp_path):
        from starlette.testclient import TestClient

        app = _make_app(tmp_path, user=_make_viewer_user())
        client = TestClient(app)

        resp = client.get("/api/sync/status/1")
        assert resp.status_code == 403
