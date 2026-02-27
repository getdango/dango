"""tests/unit/test_web_schedule_page.py

Tests for the schedule management UI page route and notification endpoints.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def _make_page_app(
    tmp_path: Path,
    user: User | None = None,
) -> Any:
    """Build a minimal FastAPI app with schedules router + ui router for templates."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from dango.exceptions import AuthorizationError
    from dango.web.routes.schedules import router
    from dango.web.routes.ui import router as ui_router

    app = FastAPI()
    app.state.project_root = tmp_path
    app.include_router(router)
    app.include_router(ui_router)

    test_user = user or _make_admin_user()

    @app.middleware("http")
    async def inject_user(request: Any, call_next: Any) -> Any:
        request.state.user = test_user
        return await call_next(request)

    @app.exception_handler(AuthorizationError)
    async def auth_error_handler(request: Any, exc: AuthorizationError) -> Any:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    return app


# ---------------------------------------------------------------------------
# Tests — Schedule page route
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSchedulesPageRoute:
    """GET /schedules."""

    def test_page_accessible_with_scheduler_view(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        app = _make_page_app(tmp_path, user=_make_viewer_user())

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/schedules")

        assert resp.status_code == 200

    def test_page_returns_html_with_correct_title(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        app = _make_page_app(tmp_path)

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/schedules")

        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Schedules" in resp.text


# ---------------------------------------------------------------------------
# Tests — Notification config endpoint
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNotificationConfigEndpoint:
    """GET /api/notifications/config."""

    def test_returns_config_when_exists(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from dango.platform.notifications.webhook import NotificationConfig, WebhookConfig

        config = NotificationConfig(
            webhooks=[
                WebhookConfig(name="slack", url="https://hooks.slack.com/test", format="slack")
            ],
            on_failure=True,
            on_success=True,
            on_stale=False,
            stale_threshold_hours=12,
        )
        app = _make_page_app(tmp_path)

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(f"{_PATCH_ROOT}.load_notification_config", return_value=config),
        ):
            client = TestClient(app)
            resp = client.get("/api/notifications/config")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["webhooks"]) == 1
        assert data["webhooks"][0]["name"] == "slack"
        assert data["on_success"] is True
        assert data["on_stale"] is False
        assert data["stale_threshold_hours"] == 12

    def test_returns_defaults_when_no_config(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        app = _make_page_app(tmp_path)

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(f"{_PATCH_ROOT}.load_notification_config", return_value=None),
        ):
            client = TestClient(app)
            resp = client.get("/api/notifications/config")

        assert resp.status_code == 200
        data = resp.json()
        assert data["webhooks"] == []
        assert data["on_failure"] is True
        assert data["on_success"] is False
        assert data["on_stale"] is True
        assert data["stale_threshold_hours"] == 24

    def test_requires_scheduler_view_permission(self, tmp_path: Path) -> None:
        """Viewer can read notification config (has scheduler.view)."""
        from fastapi.testclient import TestClient

        app = _make_page_app(tmp_path, user=_make_viewer_user())

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(f"{_PATCH_ROOT}.load_notification_config", return_value=None),
        ):
            client = TestClient(app)
            resp = client.get("/api/notifications/config")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests — Notification test endpoint
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNotificationTestEndpoint:
    """POST /api/notifications/test."""

    def test_sends_test_webhook_as_admin(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from dango.platform.notifications.webhook import NotificationConfig, WebhookConfig

        config = NotificationConfig(
            webhooks=[WebhookConfig(name="test-hook", url="https://example.com/hook")],
        )

        mock_sender = MagicMock()
        mock_sender.send = AsyncMock()

        app = _make_page_app(tmp_path)

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(f"{_PATCH_ROOT}.load_notification_config", return_value=config),
            patch(f"{_PATCH_ROOT}.WebhookSender", return_value=mock_sender),
            patch(f"{_PATCH_ROOT}.log_auth_event"),
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/notifications/test",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"
        mock_sender.send.assert_called_once()

    def test_returns_400_when_no_webhooks(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        app = _make_page_app(tmp_path)

        with (
            patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path),
            patch(f"{_PATCH_ROOT}.load_notification_config", return_value=None),
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/notifications/test",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "DANGO-S008"

    def test_requires_scheduler_manage_permission(self, tmp_path: Path) -> None:
        """Viewer cannot send test notifications (requires scheduler.manage)."""
        from fastapi.testclient import TestClient

        app = _make_page_app(tmp_path, user=_make_viewer_user())

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/notifications/test",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 403

    def test_editor_cannot_send_test(self, tmp_path: Path) -> None:
        """Editor cannot send test notifications (requires scheduler.manage)."""
        from fastapi.testclient import TestClient

        app = _make_page_app(tmp_path, user=_make_editor_user())

        with patch(f"{_PATCH_ROOT}.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.post(
                "/api/notifications/test",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 403
