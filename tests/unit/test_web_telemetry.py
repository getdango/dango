"""tests/unit/test_web_telemetry.py

Tests for dango.web.routes.telemetry — the `/settings/telemetry` web page
and its API (1.0.8-U), a second front-end onto the same telemetry state
`dango telemetry` (CLI) controls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import click
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.exceptions import DangoError
from dango.web.routes.telemetry import router

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _make_admin() -> User:
    """Create an admin user for tests."""
    return User(
        id="u-admin-1",
        email="admin@test.com",
        password_hash="hashed",
        role=Role.ADMIN,
        is_active=True,
    )


def _make_viewer() -> User:
    """Create a non-admin (viewer) user for tests."""
    return User(
        id="u-viewer-1",
        email="viewer@test.com",
        password_hash="hashed",
        role=Role.VIEWER,
        is_active=True,
    )


def _make_app(project_root: Path) -> FastAPI:
    """Create a minimal FastAPI app with the telemetry router."""
    from dango.exceptions import AuthenticationError, AuthorizationError

    app = FastAPI()
    app.state.project_root = project_root

    status_map: dict[type[DangoError], int] = {
        AuthenticationError: 401,
        AuthorizationError: 403,
        DangoError: 500,
    }

    @app.exception_handler(DangoError)
    async def dango_error_handler(request: Request, exc: DangoError) -> JSONResponse:
        status_code = 500
        for cls in type(exc).__mro__:
            if cls in status_map:
                status_code = status_map[cls]
                break
        return JSONResponse(
            status_code=status_code,
            content={"error_code": exc.error_code, "message": exc.user_message},
        )

    app.include_router(router)
    return app


def _client_as(user: User | None, project_root: Path) -> TestClient:
    """Create a test client with the given user (or None) injected via middleware."""
    app = _make_app(project_root)

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session" if user else None
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


HEADERS: dict[str, str] = {
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/json",
}


@pytest.fixture()
def admin_client(tmp_path: Path) -> TestClient:
    return _client_as(_make_admin(), tmp_path)


# ---------------------------------------------------------------------------
# GET /api/telemetry/status
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetTelemetryStatus:
    """Tests for GET /api/telemetry/status."""

    def test_returns_all_four_providers(self, admin_client: TestClient) -> None:
        with (
            patch("dango.web.routes.telemetry.is_telemetry_enabled", return_value=True),
            patch("dango.web.routes.telemetry.get_dbt_telemetry_state", return_value=False),
            patch("dango.web.routes.telemetry.get_dlt_telemetry_state", return_value=True),
            patch("dango.web.routes.telemetry.get_metabase_telemetry_state", return_value=False),
        ):
            resp = admin_client.get("/api/telemetry/status", headers=HEADERS)

        assert resp.status_code == 200
        data = resp.json()
        by_provider = {p["provider"]: p for p in data["providers"]}
        assert set(by_provider) == {"dango", "dbt", "dlt", "metabase"}
        assert by_provider["dango"]["enabled"] is True
        assert by_provider["dbt"]["enabled"] is False
        assert by_provider["dlt"]["enabled"] is True
        assert by_provider["metabase"]["enabled"] is False
        # Labels/descriptions must match the CLI's telemetry_status() table text.
        assert by_provider["dbt"]["label"] == "dbt-core"
        assert "UUID" in by_provider["dango"]["description"]

    def test_requires_authentication(self, tmp_path: Path) -> None:
        client = _client_as(None, tmp_path)
        resp = client.get("/api/telemetry/status", headers=HEADERS)
        assert resp.status_code == 401

    def test_requires_admin_permission(self, tmp_path: Path) -> None:
        client = _client_as(_make_viewer(), tmp_path)
        resp = client.get("/api/telemetry/status", headers=HEADERS)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/telemetry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSetTelemetry:
    """Tests for POST /api/telemetry."""

    def test_toggle_dango_provider(self, admin_client: TestClient) -> None:
        with patch("dango.web.routes.telemetry.set_telemetry_enabled") as mock_set:
            resp = admin_client.post(
                "/api/telemetry",
                json={"provider": "dango", "enabled": False},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        mock_set.assert_called_once_with(False)

    def test_toggle_dbt_provider(self, admin_client: TestClient) -> None:
        with patch("dango.web.routes.telemetry.set_dbt_telemetry_state") as mock_set:
            resp = admin_client.post(
                "/api/telemetry",
                json={"provider": "dbt", "enabled": True},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        mock_set.assert_called_once_with(True)

    def test_toggle_dlt_provider(self, admin_client: TestClient) -> None:
        """dlt is machine-level since 1.0.8-OPS-2 — set_dlt_telemetry_state()
        no longer takes project_root (matching set_dbt_telemetry_state())."""
        with patch("dango.web.routes.telemetry.set_dlt_telemetry_state") as mock_set:
            resp = admin_client.post(
                "/api/telemetry",
                json={"provider": "dlt", "enabled": False},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        mock_set.assert_called_once_with(False)

    def test_toggle_metabase_provider(self, admin_client: TestClient, tmp_path: Path) -> None:
        with patch("dango.web.routes.telemetry.set_metabase_telemetry") as mock_set:
            resp = admin_client.post(
                "/api/telemetry",
                json={"provider": "metabase", "enabled": True},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        mock_set.assert_called_once_with(tmp_path, True)

    def test_invalid_provider_returns_400(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/api/telemetry",
            json={"provider": "not-a-real-provider", "enabled": True},
            headers=HEADERS,
        )
        assert resp.status_code == 400

    def test_missing_enabled_returns_400(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/telemetry", json={"provider": "dango"}, headers=HEADERS)
        assert resp.status_code == 400

    def test_setter_oserror_returns_clean_error_not_500(self, admin_client: TestClient) -> None:
        with patch(
            "dango.web.routes.telemetry.set_dbt_telemetry_state",
            side_effect=OSError("disk full"),
        ):
            resp = admin_client.post(
                "/api/telemetry",
                json={"provider": "dbt", "enabled": False},
                headers=HEADERS,
            )
        assert resp.status_code == 400
        data = resp.json()
        assert "disk full" in data["message"]
        assert "Traceback" not in data["message"]

    def test_setter_click_exception_returns_clean_error(self, admin_client: TestClient) -> None:
        """Metabase's set_metabase_telemetry() raises click.ClickException
        directly (existing precedent in visualization/metabase.py) — the
        route must catch that specifically, not just Exception/RuntimeError."""
        with patch(
            "dango.web.routes.telemetry.set_metabase_telemetry",
            side_effect=click.ClickException("Metabase not configured. Run dango start first."),
        ):
            resp = admin_client.post(
                "/api/telemetry",
                json={"provider": "metabase", "enabled": False},
                headers=HEADERS,
            )
        assert resp.status_code == 400
        assert "Metabase not configured" in resp.json()["message"]

    def test_setter_unexpected_exception_returns_clean_500(self, admin_client: TestClient) -> None:
        with patch(
            "dango.web.routes.telemetry.set_dbt_telemetry_state",
            side_effect=RuntimeError("boom"),
        ):
            resp = admin_client.post(
                "/api/telemetry",
                json={"provider": "dbt", "enabled": False},
                headers=HEADERS,
            )
        assert resp.status_code == 500
        data = resp.json()
        assert "Traceback" not in data["message"]
        assert "boom" not in data["message"]

    @patch("dango.web.routes.telemetry.log_auth_event")
    def test_audit_event_logged_on_success(self, mock_log: Any, admin_client: TestClient) -> None:
        with patch("dango.web.routes.telemetry.set_telemetry_enabled"):
            admin_client.post(
                "/api/telemetry",
                json={"provider": "dango", "enabled": False},
                headers=HEADERS,
            )
        mock_log.assert_called_once()
        from dango.auth.audit import AuditEvent

        assert mock_log.call_args[0][0] == AuditEvent.TELEMETRY_TOGGLED
        assert mock_log.call_args.kwargs["details"] == {"provider": "dango", "enabled": False}

    def test_requires_admin_permission(self, tmp_path: Path) -> None:
        client = _client_as(_make_viewer(), tmp_path)
        resp = client.post(
            "/api/telemetry", json={"provider": "dango", "enabled": False}, headers=HEADERS
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /settings/telemetry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTelemetryPage:
    """Tests for GET /settings/telemetry page route."""

    def test_page_returns_html_for_admin(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/settings/telemetry", headers=HEADERS)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_page_requires_authentication(self, tmp_path: Path) -> None:
        client = _client_as(None, tmp_path)
        resp = client.get("/settings/telemetry", headers=HEADERS)
        assert resp.status_code == 401

    def test_page_requires_admin_permission(self, tmp_path: Path) -> None:
        client = _client_as(_make_viewer(), tmp_path)
        resp = client.get("/settings/telemetry", headers=HEADERS)
        assert resp.status_code == 403
