"""tests/unit/test_webhook_crud_api.py

Tests for webhook CRUD API endpoints (POST/DELETE /api/notifications/webhooks)
added by R9-I (BUG-146).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from starlette.testclient import TestClient

from dango.auth.models import Role, User

_PATCH_ROOT = "dango.web.routes.schedules"


def _make_admin_user() -> User:
    return User(id="admin-id", email="admin@test.com", role=Role.ADMIN, is_active=True)


def _make_viewer_user() -> User:
    return User(id="viewer-id", email="viewer@test.com", role=Role.VIEWER, is_active=True)


def _make_app(tmp_path: Path, user: User | None = None) -> Any:
    """Build a minimal FastAPI app with schedules router and injected user."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from dango.exceptions import AuthorizationError
    from dango.web.routes.schedules import router

    app = FastAPI()
    app.state.project_root = tmp_path
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


def _write_schedules_yml(
    tmp_path: Path,
    webhooks: list[dict[str, Any]] | None = None,
    schedules: list[dict[str, Any]] | None = None,
) -> None:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"schedules": schedules or []}
    if webhooks is not None:
        data["notifications"] = {"webhooks": webhooks}
    with open(dango_dir / "schedules.yml", "w") as f:
        yaml.dump(data, f)


def _read_webhooks(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / ".dango" / "schedules.yml"
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    result: list[dict[str, Any]] = data.get("notifications", {}).get("webhooks", [])
    return result


# ---------------------------------------------------------------------------
# POST /api/notifications/webhooks
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAddWebhook:
    """Tests for the add webhook endpoint."""

    def test_add_webhook_success(self, tmp_path: Path) -> None:
        _write_schedules_yml(tmp_path, webhooks=[])
        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            resp = client.post(
                "/api/notifications/webhooks",
                json={
                    "name": "slack_alerts",
                    "url": "https://hooks.slack.com/xxx",
                    "format": "slack",
                },
            )

        assert resp.status_code == 201
        assert resp.json()["name"] == "slack_alerts"
        webhooks = _read_webhooks(tmp_path)
        assert len(webhooks) == 1
        assert webhooks[0]["name"] == "slack_alerts"

    def test_add_webhook_creates_notifications_section(self, tmp_path: Path) -> None:
        """Adding a webhook when no notifications section exists creates it."""
        _write_schedules_yml(tmp_path)  # No webhooks key
        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            resp = client.post(
                "/api/notifications/webhooks",
                json={"name": "alerts", "url": "https://example.com/hook", "format": "generic"},
            )

        assert resp.status_code == 201
        webhooks = _read_webhooks(tmp_path)
        assert len(webhooks) == 1

    def test_add_webhook_preserves_existing_schedules(self, tmp_path: Path) -> None:
        """Adding a webhook must not clobber the schedules section."""
        schedules = [{"name": "daily", "type": "sync", "cron": "0 6 * * *", "sources": ["src"]}]
        _write_schedules_yml(tmp_path, webhooks=[], schedules=schedules)
        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client.post(
                "/api/notifications/webhooks",
                json={"name": "alerts", "url": "https://example.com/hook", "format": "generic"},
            )

        path = tmp_path / ".dango" / "schedules.yml"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert len(data["schedules"]) == 1
        assert data["schedules"][0]["name"] == "daily"

    def test_add_webhook_duplicate_returns_409(self, tmp_path: Path) -> None:
        _write_schedules_yml(
            tmp_path,
            webhooks=[{"name": "existing", "url": "https://example.com", "format": "generic"}],
        )
        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            resp = client.post(
                "/api/notifications/webhooks",
                json={"name": "existing", "url": "https://example.com/new", "format": "generic"},
            )

        assert resp.status_code == 409

    def test_add_webhook_invalid_name(self, tmp_path: Path) -> None:
        _write_schedules_yml(tmp_path, webhooks=[])
        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            resp = client.post(
                "/api/notifications/webhooks",
                json={"name": "Bad-Name!", "url": "https://example.com", "format": "generic"},
            )

        assert resp.status_code == 400

    def test_add_webhook_invalid_url(self, tmp_path: Path) -> None:
        _write_schedules_yml(tmp_path, webhooks=[])
        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            resp = client.post(
                "/api/notifications/webhooks",
                json={"name": "valid_name", "url": "ftp://bad-protocol.com", "format": "generic"},
            )

        assert resp.status_code == 400

    def test_add_webhook_invalid_format(self, tmp_path: Path) -> None:
        _write_schedules_yml(tmp_path, webhooks=[])
        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            resp = client.post(
                "/api/notifications/webhooks",
                json={"name": "valid_name", "url": "https://example.com", "format": "teams"},
            )

        assert resp.status_code == 400

    def test_add_webhook_viewer_forbidden(self, tmp_path: Path) -> None:
        _write_schedules_yml(tmp_path, webhooks=[])
        app = _make_app(tmp_path, user=_make_viewer_user())
        client = TestClient(app)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            resp = client.post(
                "/api/notifications/webhooks",
                json={"name": "test", "url": "https://example.com", "format": "generic"},
            )

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /api/notifications/webhooks/{name}
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeleteWebhook:
    """Tests for the delete webhook endpoint."""

    def test_delete_webhook_success(self, tmp_path: Path) -> None:
        _write_schedules_yml(
            tmp_path,
            webhooks=[
                {"name": "alerts", "url": "https://example.com", "format": "generic"},
                {"name": "keep_me", "url": "https://other.com", "format": "slack"},
            ],
        )
        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            resp = client.delete("/api/notifications/webhooks/alerts")

        assert resp.status_code == 200
        webhooks = _read_webhooks(tmp_path)
        assert len(webhooks) == 1
        assert webhooks[0]["name"] == "keep_me"

    def test_delete_webhook_not_found(self, tmp_path: Path) -> None:
        _write_schedules_yml(tmp_path, webhooks=[])
        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            resp = client.delete("/api/notifications/webhooks/nonexistent")

        assert resp.status_code == 404

    def test_delete_webhook_viewer_forbidden(self, tmp_path: Path) -> None:
        _write_schedules_yml(
            tmp_path,
            webhooks=[{"name": "alerts", "url": "https://example.com", "format": "generic"}],
        )
        app = _make_app(tmp_path, user=_make_viewer_user())
        client = TestClient(app)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            resp = client.delete("/api/notifications/webhooks/alerts")

        assert resp.status_code == 403
