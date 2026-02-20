"""tests/unit/test_web_users.py

Tests for admin user management endpoints in dango/web/routes/users.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.models import Role, User, UserUpdate
from dango.auth.security import hash_password
from dango.exceptions import DangoError
from dango.migrations.runner import MigrationRunner
from dango.web.routes.users import router

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    """Create a fresh auth database at the standard project path."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    db_path = dango_dir / "auth.db"
    migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    runner = MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=migrations_dir)
    runner.apply_pending()
    return db_path


def _make_user(
    db_path: Path,
    email: str = "test@example.com",
    password: str = "securepassword123",
    role: Role = Role.EDITOR,
    **overrides: Any,
) -> User:
    """Create and persist a user, returning the model."""
    defaults: dict[str, Any] = {
        "email": email,
        "password_hash": hash_password(password),
        "role": role,
    }
    defaults.update(overrides)
    user = User(**defaults)
    db.create_user(db_path, user)
    return user


def _make_app(db_path: Path) -> FastAPI:
    """Create a minimal FastAPI app with users routes + error handlers."""
    from dango.exceptions import (
        AuthenticationError,
        AuthorizationError,
        UserExistsError,
        UserNotFoundError,
    )

    app = FastAPI()
    app.state.project_root = db_path.parent.parent

    # Register the DangoError handler so require_permission works correctly
    status_map: dict[type[DangoError], int] = {
        AuthenticationError: 401,
        AuthorizationError: 403,
        UserNotFoundError: 404,
        UserExistsError: 409,
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


def _make_client(app: FastAPI) -> TestClient:
    """Create a test client."""
    return TestClient(app, raise_server_exceptions=False)


def _auth_headers() -> dict[str, str]:
    """Standard headers for authenticated requests."""
    return {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}


def _setup_admin_client(
    tmp_path: Path,
    **user_overrides: Any,
) -> tuple[TestClient, Path, User]:
    """Set up a test client with an admin user injected via middleware."""
    db_path = _make_db(tmp_path)
    defaults: dict[str, Any] = {"email": "admin@example.com", "role": Role.ADMIN}
    defaults.update(user_overrides)
    admin = _make_user(db_path, **defaults)
    app = _make_app(db_path)
    client = _make_client(app)

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = admin
        request.state.auth_method = "session"
        return await call_next(request)

    return client, db_path, admin


def _setup_viewer_client(
    tmp_path: Path,
) -> tuple[TestClient, Path, User]:
    """Set up a test client with a viewer user (non-admin)."""
    db_path = _make_db(tmp_path)
    viewer = _make_user(db_path, email="viewer@example.com", role=Role.VIEWER)
    app = _make_app(db_path)
    client = _make_client(app)

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = viewer
        request.state.auth_method = "session"
        return await call_next(request)

    return client, db_path, viewer


# ---------------------------------------------------------------------------
# Admin user management tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdminCreateUser:
    """Tests for GET/POST /api/admin/users."""

    def test_list_users(self, tmp_path: Path) -> None:
        """Returns all users as JSON array."""
        client, db_path, admin = _setup_admin_client(tmp_path)
        _make_user(db_path, email="user2@example.com", role=Role.VIEWER)
        resp = client.get("/api/admin/users", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        emails = {u["email"] for u in data}
        assert "admin@example.com" in emails
        assert "user2@example.com" in emails

    def test_create_user_success(self, tmp_path: Path) -> None:
        """Creates user and returns temp_password."""
        client, db_path, _admin = _setup_admin_client(tmp_path)

        resp = client.post(
            "/api/admin/users",
            json={"email": "new@example.com", "role": "editor"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["user"]["email"] == "new@example.com"
        assert data["user"]["role"] == "editor"
        assert "temp_password" in data
        assert len(data["temp_password"]) > 0

    def test_create_user_duplicate(self, tmp_path: Path) -> None:
        """Duplicate email returns 409."""
        client, db_path, admin = _setup_admin_client(tmp_path)

        resp = client.post(
            "/api/admin/users",
            json={"email": "admin@example.com"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 409

    def test_create_user_invalid_role(self, tmp_path: Path) -> None:
        """Invalid role returns 400."""
        client, _db_path, _admin = _setup_admin_client(tmp_path)

        resp = client.post(
            "/api/admin/users",
            json={"email": "x@example.com", "role": "superadmin"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 400
        assert "Invalid role" in resp.json()["message"]

    def test_create_user_invalid_body(self, tmp_path: Path) -> None:
        """Malformed body returns 400."""
        client, _db_path, _admin = _setup_admin_client(tmp_path)

        resp = client.post("/api/admin/users", content=b"not json", headers=_auth_headers())
        assert resp.status_code == 400


@pytest.mark.unit
class TestAdminChangeRole:
    """Tests for PUT /api/admin/users/{user_id}/role."""

    def test_change_role(self, tmp_path: Path) -> None:
        """Successfully changes user role."""
        client, db_path, _admin = _setup_admin_client(tmp_path)
        user = _make_user(db_path, email="user@example.com", role=Role.VIEWER)

        resp = client.put(
            f"/api/admin/users/{user.id}/role",
            json={"role": "editor"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "editor"

    def test_change_role_last_admin(self, tmp_path: Path) -> None:
        """Cannot demote the only admin."""
        client, _db_path, admin = _setup_admin_client(tmp_path)

        resp = client.put(
            f"/api/admin/users/{admin.id}/role",
            json={"role": "viewer"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 409
        assert "only active admin" in resp.json()["message"].lower()

    def test_change_role_not_found(self, tmp_path: Path) -> None:
        """Non-existent user returns 404."""
        client, _db_path, _admin = _setup_admin_client(tmp_path)

        resp = client.put(
            "/api/admin/users/nonexistent-id/role",
            json={"role": "editor"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 404


@pytest.mark.unit
class TestAdminResetPassword:
    """Tests for POST /api/admin/users/{user_id}/reset-password."""

    def test_reset_password(self, tmp_path: Path) -> None:
        """Reset returns a new temp password and invalidates sessions."""
        client, db_path, _admin = _setup_admin_client(tmp_path)
        user = _make_user(db_path, email="user@example.com")

        resp = client.post(
            f"/api/admin/users/{user.id}/reset-password",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "temp_password" in data
        assert data["user"]["must_change_password"] is True

    def test_reset_password_not_found(self, tmp_path: Path) -> None:
        """Non-existent user returns 404."""
        client, _db_path, _admin = _setup_admin_client(tmp_path)

        resp = client.post(
            "/api/admin/users/nonexistent-id/reset-password",
            headers=_auth_headers(),
        )
        assert resp.status_code == 404


@pytest.mark.unit
class TestAdminDeactivateUser:
    """Tests for POST /api/admin/users/{user_id}/deactivate."""

    def test_deactivate_user(self, tmp_path: Path) -> None:
        """Deactivates a user."""
        client, db_path, _admin = _setup_admin_client(tmp_path)
        user = _make_user(db_path, email="user@example.com")

        resp = client.post(
            f"/api/admin/users/{user.id}/deactivate",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        updated = db.get_user_by_id(db_path, user.id)
        assert updated is not None
        assert updated.is_active is False

    def test_deactivate_self(self, tmp_path: Path) -> None:
        """Cannot deactivate own account."""
        client, _db_path, admin = _setup_admin_client(tmp_path)

        resp = client.post(
            f"/api/admin/users/{admin.id}/deactivate",
            headers=_auth_headers(),
        )
        assert resp.status_code == 409
        assert "own account" in resp.json()["message"].lower()


@pytest.mark.unit
class TestAdminReactivateUser:
    """Tests for POST /api/admin/users/{user_id}/reactivate."""

    def test_reactivate_user(self, tmp_path: Path) -> None:
        """Reactivates an inactive user."""
        client, db_path, _admin = _setup_admin_client(tmp_path)
        user = _make_user(db_path, email="user@example.com", is_active=False)

        resp = client.post(
            f"/api/admin/users/{user.id}/reactivate",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True


@pytest.mark.unit
class TestAdminDeleteUser:
    """Tests for DELETE /api/admin/users/{user_id}."""

    def test_delete_user_success(self, tmp_path: Path) -> None:
        """Deletes a user with correct email confirmation."""
        client, db_path, _admin = _setup_admin_client(tmp_path)
        user = _make_user(db_path, email="doomed@example.com")

        resp = client.request(
            "DELETE",
            f"/api/admin/users/{user.id}",
            json={"confirm_email": "doomed@example.com"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert db.get_user_by_id(db_path, user.id) is None

    def test_delete_user_wrong_email(self, tmp_path: Path) -> None:
        """Wrong confirmation email returns 400."""
        client, db_path, _admin = _setup_admin_client(tmp_path)
        user = _make_user(db_path, email="user@example.com")

        resp = client.request(
            "DELETE",
            f"/api/admin/users/{user.id}",
            json={"confirm_email": "wrong@example.com"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 400

    def test_delete_user_self(self, tmp_path: Path) -> None:
        """Cannot delete own account."""
        client, _db_path, admin = _setup_admin_client(tmp_path)

        resp = client.request(
            "DELETE",
            f"/api/admin/users/{admin.id}",
            json={"confirm_email": "admin@example.com"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 409
        assert "own account" in resp.json()["message"].lower()

    def test_delete_not_found(self, tmp_path: Path) -> None:
        """Non-existent user returns 404."""
        client, _db_path, _admin = _setup_admin_client(tmp_path)
        resp = client.request(
            "DELETE",
            "/api/admin/users/nonexistent-id",
            json={"confirm_email": "x@example.com"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 404


@pytest.mark.unit
class TestAdminUnlockUser:
    """Tests for POST /api/admin/users/{user_id}/unlock."""

    def test_unlock_user(self, tmp_path: Path) -> None:
        """Unlocks a locked user."""
        client, db_path, _admin = _setup_admin_client(tmp_path)
        user = _make_user(db_path, email="locked@example.com")
        locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
        db.update_user(
            db_path,
            user.id,
            UserUpdate(failed_login_attempts=5, locked_until=locked_until),
        )

        resp = client.post(
            f"/api/admin/users/{user.id}/unlock",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

        updated = db.get_user_by_id(db_path, user.id)
        assert updated is not None
        assert updated.failed_login_attempts == 0
        assert updated.locked_until is None


@pytest.mark.unit
class TestAdminRevokeSessions:
    """Tests for POST /api/admin/users/{user_id}/revoke-sessions."""

    def test_revoke_sessions(self, tmp_path: Path) -> None:
        """Revokes all sessions for a user."""
        client, db_path, _admin = _setup_admin_client(tmp_path)
        user = _make_user(db_path, email="user@example.com")

        resp = client.post(
            f"/api/admin/users/{user.id}/revoke-sessions",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "revoked_count" in data


@pytest.mark.unit
class TestNonAdminForbidden:
    """Test that non-admin users get 403 on all admin endpoints."""

    def test_viewer_forbidden_list(self, tmp_path: Path) -> None:
        """Viewer cannot list users."""
        client, _db_path, _viewer = _setup_viewer_client(tmp_path)
        resp = client.get("/api/admin/users", headers=_auth_headers())
        assert resp.status_code == 403

    def test_viewer_forbidden_create(self, tmp_path: Path) -> None:
        """Viewer cannot create users."""
        client, _db_path, _viewer = _setup_viewer_client(tmp_path)
        resp = client.post(
            "/api/admin/users",
            json={"email": "new@example.com"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Settings page tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSettingsPages:
    """Tests for page routes."""

    def test_admin_page_renders(self, tmp_path: Path) -> None:
        """Admin can access /settings/users."""
        client, _db_path, _admin = _setup_admin_client(tmp_path)
        resp = client.get("/settings/users")
        assert resp.status_code == 200
        assert "User Management" in resp.text

    def test_admin_page_forbidden_for_viewer(self, tmp_path: Path) -> None:
        """Viewer gets 403 on /settings/users."""
        client, _db_path, _viewer = _setup_viewer_client(tmp_path)
        resp = client.get("/settings/users")
        assert resp.status_code == 403

    def test_account_page_renders(self, tmp_path: Path) -> None:
        """Authenticated user can access /settings/account."""
        client, _db_path, admin = _setup_admin_client(tmp_path)
        resp = client.get("/settings/account")
        assert resp.status_code == 200
        assert "Account Settings" in resp.text

    def test_account_page_unauthenticated(self, tmp_path: Path) -> None:
        """Unauthenticated user gets redirected to /login."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = None
            request.state.auth_method = None
            return await call_next(request)

        client = _make_client(app)
        resp = client.get("/settings/account", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"
