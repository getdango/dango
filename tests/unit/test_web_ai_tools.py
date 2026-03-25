"""tests/unit/test_web_ai_tools.py

Tests for dango.web.routes.ai — tools endpoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.exceptions import (
    AuthenticationError,
    AuthorizationError,
    DangoError,
    ValidationError,
)
from dango.web.routes.ai import router

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _make_user(role: Role = Role.ADMIN) -> User:
    """Create a test user."""
    return User(
        id="u-test-1",
        email="test@test.com",
        password_hash="hashed",
        role=role,
        is_active=True,
    )


def _make_app(project_root: Path) -> FastAPI:
    """Create a minimal FastAPI app with the AI router."""
    app = FastAPI()
    app.state.project_root = project_root

    status_map: dict[type[DangoError], int] = {
        AuthenticationError: 401,
        AuthorizationError: 403,
        ValidationError: 400,
        DangoError: 500,
    }

    @app.exception_handler(DangoError)
    async def dango_error_handler(
        request: Request,
        exc: DangoError,
    ) -> JSONResponse:
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
    """Create a test client with auth middleware injecting a user."""
    user = _make_user(role)
    app = _make_app(tmp_path)

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    client = TestClient(app, raise_server_exceptions=False)
    return client, tmp_path


def _setup_unauthenticated_client(tmp_path: Path) -> TestClient:
    """Create a test client with no user set (unauthenticated)."""
    app = _make_app(tmp_path)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /api/tools
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetTools:
    """Tests for GET /api/tools."""

    @patch("dango.web.routes.ai.log_auth_event")
    def test_returns_tool_list(
        self,
        mock_audit: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Returns non-empty tool list for admin."""
        client, _ = _setup_client(tmp_path)
        resp = client.get("/api/tools")
        assert resp.status_code == 200
        assert len(resp.json()["tools"]) > 0

    @patch("dango.web.routes.ai.log_auth_event")
    def test_tool_has_required_fields(
        self,
        mock_audit: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Each tool has all required fields."""
        client, _ = _setup_client(tmp_path)
        resp = client.get("/api/tools")
        data = resp.json()

        required_fields = {
            "name",
            "description",
            "endpoint",
            "method",
            "parameters",
            "permissions_required",
            "is_read_only",
            "is_safe_to_retry",
        }
        for tool in data["tools"]:
            assert required_fields.issubset(tool.keys()), (
                f"Tool {tool.get('name')} missing: {required_fields - tool.keys()}"
            )

    @patch("dango.web.routes.ai.log_auth_event")
    def test_mutating_tools_flagged(
        self,
        mock_audit: MagicMock,
        tmp_path: Path,
    ) -> None:
        """sync_source and run_dbt_model are not read-only."""
        client, _ = _setup_client(tmp_path)
        resp = client.get("/api/tools")
        mutating = {t["name"] for t in resp.json()["tools"] if not t["is_read_only"]}
        assert "sync_source" in mutating
        assert "run_dbt_model" in mutating

    @patch("dango.web.routes.ai.log_auth_event")
    def test_all_roles_see_all_tools(
        self,
        mock_audit: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Viewer, editor, and admin all see the same tools."""
        tool_counts = []
        for role in [Role.VIEWER, Role.EDITOR, Role.ADMIN]:
            client, _ = _setup_client(tmp_path, role=role)
            resp = client.get("/api/tools")
            assert resp.status_code == 200
            tool_counts.append(len(resp.json()["tools"]))
        assert tool_counts[0] == tool_counts[1] == tool_counts[2]

    @patch("dango.web.routes.ai.log_auth_event")
    def test_mutating_tools_have_permissions(
        self,
        mock_audit: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Mutating tools include correct permissions_required."""
        client, _ = _setup_client(tmp_path)
        resp = client.get("/api/tools")
        tools_by_name = {t["name"]: t for t in resp.json()["tools"]}
        assert "source.sync" in tools_by_name["sync_source"]["permissions_required"]
        assert "dbt.run" in tools_by_name["run_dbt_model"]["permissions_required"]

    @patch("dango.web.routes.ai.log_auth_event")
    def test_audit_event_logged(
        self,
        mock_audit: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Audit event is logged with endpoint detail."""
        client, _ = _setup_client(tmp_path)
        client.get("/api/tools")
        mock_audit.assert_called_once()
        from dango.auth.audit import AuditEvent

        assert mock_audit.call_args[0][0] == AuditEvent.AI_CATALOG_VIEWED
        assert mock_audit.call_args[1]["details"] == {"endpoint": "tools"}

    def test_unauthenticated_returns_401(self, tmp_path: Path) -> None:
        """Unauthenticated request returns 401."""
        client = _setup_unauthenticated_client(tmp_path)
        resp = client.get("/api/tools")
        assert resp.status_code == 401

    @patch("dango.web.routes.ai.log_auth_event")
    def test_has_description_field(
        self,
        mock_audit: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Response has self-describing description field."""
        client, _ = _setup_client(tmp_path)
        resp = client.get("/api/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert "description" in data
        assert len(data["description"]) > 0

    @patch("dango.web.routes.ai.log_auth_event")
    def test_user_role_included(
        self,
        mock_audit: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Response includes user_role field."""
        client, _ = _setup_client(tmp_path, role=Role.VIEWER)
        resp = client.get("/api/tools")
        assert resp.status_code == 200
        assert resp.json()["user_role"] == "viewer"
