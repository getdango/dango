"""tests/unit/test_governance_api.py

Unit tests for governance web API endpoints (dango/web/routes/governance.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.exceptions import AuthorizationError, DangoError
from dango.web.routes.governance import router

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _make_user(role: Role = Role.ADMIN) -> User:
    return User(
        id="u-test-1",
        email="test@test.com",
        password_hash="hashed",
        role=role,
        is_active=True,
    )


def _make_app(project_root: Path) -> FastAPI:
    app = FastAPI()
    app.state.project_root = project_root

    @app.exception_handler(DangoError)
    async def dango_error_handler(request: Request, exc: DangoError) -> JSONResponse:
        status_map: dict[type[DangoError], int] = {
            AuthorizationError: 403,
            DangoError: 500,
        }
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


def _setup_client(
    tmp_path: Path,
    role: Role = Role.ADMIN,
) -> tuple[TestClient, Path]:
    user = _make_user(role)
    app = _make_app(tmp_path)

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    client = TestClient(app, raise_server_exceptions=False)
    return client, tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAcceptDriftEndpoint:
    """Tests for POST /api/governance/drift/{source}/accept."""

    def test_accept_drift_returns_200(self, tmp_path: Path) -> None:
        """Admin can accept drift for a source."""
        client, _ = _setup_client(tmp_path, Role.ADMIN)
        mock_attention = [
            {"source": "shopify", "reason": "1 breaking", "drift_events": [], "created_at": "t"}
        ]
        with (
            patch(
                "dango.governance.schema_drift.accept_drift",
            ) as mock_accept,
            patch(
                "dango.governance.schema_drift.get_sources_needing_attention",
                return_value=mock_attention,
            ),
            patch("dango.web.routes.governance.log_auth_event"),
            patch("dango.web.routes.governance.get_project_root", return_value=tmp_path),
        ):
            resp = client.post(
                "/api/governance/drift/shopify/accept",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "shopify"
        assert data["accepted"] is True
        mock_accept.assert_called_once()

    def test_accept_drift_404_when_no_attention(self, tmp_path: Path) -> None:
        """Returns 404 when source has no pending drift."""
        client, _ = _setup_client(tmp_path, Role.ADMIN)
        with (
            patch(
                "dango.governance.schema_drift.get_sources_needing_attention",
                return_value=[],
            ),
            patch("dango.web.routes.governance.log_auth_event"),
            patch("dango.web.routes.governance.get_project_root", return_value=tmp_path),
        ):
            resp = client.post(
                "/api/governance/drift/nonexistent/accept",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert resp.status_code == 404

    def test_accept_drift_requires_manage_permission(self, tmp_path: Path) -> None:
        """Viewer gets 403 on accept (needs governance.manage)."""
        client, _ = _setup_client(tmp_path, Role.VIEWER)
        with (
            patch("dango.web.routes.governance.log_auth_event"),
            patch("dango.web.routes.governance.get_project_root", return_value=tmp_path),
        ):
            resp = client.post(
                "/api/governance/drift/shopify/accept",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert resp.status_code == 403


@pytest.mark.unit
class TestAttentionEndpoint:
    """Tests for GET /api/governance/attention."""

    def test_attention_returns_sources(self, tmp_path: Path) -> None:
        """Returns attention sources."""
        client, _ = _setup_client(tmp_path, Role.ADMIN)
        mock_rows = [
            {
                "source": "shopify",
                "reason": "1 breaking change(s)",
                "drift_events": [],
                "created_at": "2026-01-01",
            }
        ]
        with (
            patch(
                "dango.governance.schema_drift.get_sources_needing_attention",
                return_value=mock_rows,
            ),
            patch("dango.web.routes.governance.get_project_root", return_value=tmp_path),
        ):
            resp = client.get("/api/governance/attention")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["source"] == "shopify"

    def test_attention_empty(self, tmp_path: Path) -> None:
        """Returns empty list when no attention needed."""
        client, _ = _setup_client(tmp_path, Role.ADMIN)
        with (
            patch(
                "dango.governance.schema_drift.get_sources_needing_attention",
                return_value=[],
            ),
            patch("dango.web.routes.governance.get_project_root", return_value=tmp_path),
        ):
            resp = client.get("/api/governance/attention")

        assert resp.status_code == 200
        assert resp.json() == []
