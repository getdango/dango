"""tests/unit/test_auth_permissions.py

Tests for the RBAC permission system in dango/auth/permissions.py.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from dango.auth.models import Role, User
from dango.auth.permissions import (
    PERMISSIONS,
    ROLE_PERMISSIONS,
    check_permission,
    get_permissions,
    has_permission,
    require_permission,
)
from dango.exceptions import AuthenticationError, AuthorizationError, DangoError


def _make_user(**overrides: Any) -> User:
    """Build a User model with sensible defaults, applying overrides."""
    defaults: dict[str, Any] = {
        "id": "user-001",
        "email": "test@example.com",
        "password_hash": "hashed",
        "role": Role.EDITOR,
        "is_active": True,
    }
    defaults.update(overrides)
    return User(**defaults)


# ---------------------------------------------------------------------------
# Permission registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPermissionRegistry:
    """Tests for the PERMISSIONS frozenset."""

    def test_is_frozenset(self) -> None:
        assert isinstance(PERMISSIONS, frozenset)

    def test_permission_count(self) -> None:
        assert len(PERMISSIONS) == 29

    def test_all_use_dot_separator(self) -> None:
        for perm in PERMISSIONS:
            assert "." in perm, f"Permission {perm!r} lacks a dot separator"

    def test_no_wildcard_in_registry(self) -> None:
        assert "*" not in PERMISSIONS


# ---------------------------------------------------------------------------
# Role → permission mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRolePermissions:
    """Tests for the ROLE_PERMISSIONS mapping."""

    def test_all_roles_present(self) -> None:
        for role in Role:
            assert role in ROLE_PERMISSIONS

    def test_admin_has_wildcard(self) -> None:
        assert "*" in ROLE_PERMISSIONS[Role.ADMIN]

    def test_editor_has_no_wildcard(self) -> None:
        assert "*" not in ROLE_PERMISSIONS[Role.EDITOR]

    def test_viewer_has_no_wildcard(self) -> None:
        assert "*" not in ROLE_PERMISSIONS[Role.VIEWER]

    def test_viewer_is_subset_of_editor(self) -> None:
        """Viewer permissions should be a subset of editor permissions.

        Exception: governance.view is viewer-only — viewers get read-only
        audit/PII access for separation-of-duties oversight, while editors
        (who modify data) do not.
        """
        viewer = ROLE_PERMISSIONS[Role.VIEWER]
        editor = ROLE_PERMISSIONS[Role.EDITOR]
        viewer_minus_special = viewer - {"governance.view"}
        assert viewer_minus_special.issubset(editor)

    def test_editor_cannot_manage_users(self) -> None:
        editor_perms = ROLE_PERMISSIONS[Role.EDITOR]
        assert "users.manage" not in editor_perms
        assert "auth.manage" not in editor_perms
        assert "audit.view" not in editor_perms

    def test_editor_cannot_manage_platform(self) -> None:
        editor_perms = ROLE_PERMISSIONS[Role.EDITOR]
        assert "platform.manage" not in editor_perms
        assert "config.manage" not in editor_perms

    def test_viewer_cannot_sync_upload_query_or_manage(self) -> None:
        viewer_perms = ROLE_PERMISSIONS[Role.VIEWER]
        assert "source.sync" not in viewer_perms
        assert "csv.upload" not in viewer_perms
        assert "csv.delete" not in viewer_perms
        assert "dbt.run" not in viewer_perms
        assert "query.execute" not in viewer_perms
        assert "users.manage" not in viewer_perms

    def test_all_role_permissions_are_valid(self) -> None:
        """Every permission in a role mapping must be in the registry or be '*'."""
        for role, perms in ROLE_PERMISSIONS.items():
            for perm in perms:
                assert perm == "*" or perm in PERMISSIONS, (
                    f"Role {role.value} has unknown permission {perm!r}"
                )


# ---------------------------------------------------------------------------
# get_permissions()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetPermissions:
    """Tests for get_permissions()."""

    def test_admin_returns_wildcard(self) -> None:
        result = get_permissions(Role.ADMIN)
        assert "*" in result

    def test_viewer_returns_viewer_perms(self) -> None:
        result = get_permissions(Role.VIEWER)
        assert result == ROLE_PERMISSIONS[Role.VIEWER]

    def test_editor_returns_editor_perms(self) -> None:
        result = get_permissions(Role.EDITOR)
        assert result == ROLE_PERMISSIONS[Role.EDITOR]


# ---------------------------------------------------------------------------
# has_permission()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHasPermission:
    """Tests for has_permission()."""

    def test_admin_has_any_permission(self) -> None:
        user = _make_user(role=Role.ADMIN)
        assert has_permission(user, "source.sync") is True
        assert has_permission(user, "users.manage") is True

    def test_editor_has_granted_permission(self) -> None:
        user = _make_user(role=Role.EDITOR)
        assert has_permission(user, "source.sync") is True

    def test_editor_denied_ungranted_permission(self) -> None:
        user = _make_user(role=Role.EDITOR)
        assert has_permission(user, "users.manage") is False

    def test_viewer_has_view_permission(self) -> None:
        user = _make_user(role=Role.VIEWER)
        assert has_permission(user, "source.view") is True

    def test_viewer_denied_write_permission(self) -> None:
        user = _make_user(role=Role.VIEWER)
        assert has_permission(user, "source.sync") is False

    def test_inactive_user_denied(self) -> None:
        user = _make_user(role=Role.ADMIN, is_active=False)
        assert has_permission(user, "source.view") is False

    def test_unknown_permission_denied(self) -> None:
        user = _make_user(role=Role.ADMIN)
        assert has_permission(user, "nonexistent.perm") is False

    def test_unknown_permission_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        user = _make_user(role=Role.ADMIN)
        with caplog.at_level(logging.WARNING, logger="dango.auth.permissions"):
            has_permission(user, "nonexistent.perm")
        assert "Unknown permission checked: nonexistent.perm" in caplog.text


# ---------------------------------------------------------------------------
# check_permission()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckPermission:
    """Tests for check_permission()."""

    def test_allowed_does_not_raise(self) -> None:
        user = _make_user(role=Role.ADMIN)
        check_permission(user, "source.sync")  # should not raise

    def test_denied_raises_authorization_error(self) -> None:
        user = _make_user(role=Role.VIEWER)
        with pytest.raises(AuthorizationError):
            check_permission(user, "users.manage")

    def test_error_has_context_fields(self) -> None:
        user = _make_user(role=Role.VIEWER, id="user-xyz")
        with pytest.raises(AuthorizationError) as exc_info:
            check_permission(user, "users.manage")
        ctx = exc_info.value.context
        assert ctx["permission"] == "users.manage"
        assert ctx["user_id"] == "user-xyz"
        assert ctx["role"] == "viewer"

    def test_error_code_is_dango_s002(self) -> None:
        user = _make_user(role=Role.VIEWER)
        with pytest.raises(AuthorizationError) as exc_info:
            check_permission(user, "users.manage")
        assert exc_info.value.error_code == "DANGO-S002"

    def test_inactive_user_raises(self) -> None:
        user = _make_user(role=Role.ADMIN, is_active=False)
        with pytest.raises(AuthorizationError):
            check_permission(user, "source.view")


# ---------------------------------------------------------------------------
# require_permission() FastAPI dependency
# ---------------------------------------------------------------------------


_ERROR_STATUS: dict[type[DangoError], int] = {
    AuthenticationError: 401,
    AuthorizationError: 403,
}


def _create_test_app() -> FastAPI:
    """Build a minimal FastAPI app with a protected route."""
    app = FastAPI()

    @app.exception_handler(DangoError)
    async def _handle_dango_error(request: Request, exc: DangoError) -> JSONResponse:
        status = 500
        for cls in type(exc).__mro__:
            if cls in _ERROR_STATUS:
                status = _ERROR_STATUS[cls]
                break
        return JSONResponse(status_code=status, content={"error": exc.error_code})

    @app.middleware("http")
    async def inject_user(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Simulate auth middleware by reading X-Test-User-Role header."""
        role_header = request.headers.get("x-test-user-role")
        active_header = request.headers.get("x-test-user-active", "true")
        if role_header:
            request.state.user = _make_user(
                role=Role(role_header),
                is_active=active_header.lower() == "true",
            )
        return await call_next(request)

    @app.get("/protected")
    async def protected_route(
        user: User = Depends(require_permission("source.sync")),
    ) -> dict[str, str]:
        return {"user_id": user.id, "email": user.email}

    return app


@pytest.mark.unit
class TestRequirePermission:
    """Tests for the require_permission() FastAPI dependency."""

    def setup_method(self) -> None:
        self.client = TestClient(_create_test_app(), raise_server_exceptions=False)

    def test_authenticated_and_permitted(self) -> None:
        resp = self.client.get("/protected", headers={"x-test-user-role": "editor"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == "user-001"

    def test_authenticated_but_denied(self) -> None:
        resp = self.client.get("/protected", headers={"x-test-user-role": "viewer"})
        assert resp.status_code == 403

    def test_no_user_returns_401(self) -> None:
        resp = self.client.get("/protected")
        assert resp.status_code == 401

    def test_inactive_user_returns_403(self) -> None:
        resp = self.client.get(
            "/protected",
            headers={"x-test-user-role": "admin", "x-test-user-active": "false"},
        )
        assert resp.status_code == 403

    def test_admin_permitted(self) -> None:
        resp = self.client.get("/protected", headers={"x-test-user-role": "admin"})
        assert resp.status_code == 200
