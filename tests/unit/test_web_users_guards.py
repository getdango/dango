"""tests/unit/test_web_users_guards.py

Edge-case tests for admin user management guard logic in
dango/web/routes/users.py.  Covers happy-path scenarios that the main
test_web_users.py could not fit within its line budget.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.models import Role, User
from dango.auth.security import hash_password
from dango.exceptions import DangoError
from dango.migrations.runner import MigrationRunner
from dango.web.routes.users import router

# ---------------------------------------------------------------------------
# Test infrastructure (mirrors test_web_users.py patterns)
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
    role: Role = Role.EDITOR,
    **overrides: Any,
) -> User:
    """Create and persist a user, returning the model."""
    defaults: dict[str, Any] = {
        "email": email,
        "password_hash": hash_password("securepassword123"),
        "role": role,
    }
    defaults.update(overrides)
    user = User(**defaults)
    db.create_user(db_path, user)
    return user


def _make_app(db_path: Path, acting_user: User) -> tuple[FastAPI, TestClient]:
    """Create a minimal FastAPI app with the acting user injected via middleware."""
    from dango.exceptions import (
        AuthenticationError,
        AuthorizationError,
        UserExistsError,
        UserNotFoundError,
    )

    app = FastAPI()
    app.state.project_root = db_path.parent.parent

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

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = acting_user
        request.state.auth_method = "session"
        return await call_next(request)

    return app, TestClient(app, raise_server_exceptions=False)


_HEADERS: dict[str, str] = {
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUserGuardEdgeCases:
    """Happy-path guard scenarios for admin user management."""

    def test_self_role_change_not_last_admin(self, tmp_path: Path) -> None:
        """Admin can change own role when other admins exist."""
        db_path = _make_db(tmp_path)
        admin1 = _make_user(db_path, email="admin1@example.com", role=Role.ADMIN)
        _make_user(db_path, email="admin2@example.com", role=Role.ADMIN)
        _app, client = _make_app(db_path, admin1)

        resp = client.put(
            f"/api/admin/users/{admin1.id}/role",
            json={"role": "editor"},
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "editor"

    def test_reactivate_already_active(self, tmp_path: Path) -> None:
        """Reactivating an already-active user is idempotent (200)."""
        db_path = _make_db(tmp_path)
        admin = _make_user(db_path, email="admin@example.com", role=Role.ADMIN)
        target = _make_user(db_path, email="active@example.com", role=Role.EDITOR)
        _app, client = _make_app(db_path, admin)

        resp = client.post(
            f"/api/admin/users/{target.id}/reactivate",
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_active"] is True

    def test_deactivate_admin_when_multiple_admins(self, tmp_path: Path) -> None:
        """Admin can deactivate another admin when 2+ admins exist."""
        db_path = _make_db(tmp_path)
        admin1 = _make_user(db_path, email="admin1@example.com", role=Role.ADMIN)
        admin2 = _make_user(db_path, email="admin2@example.com", role=Role.ADMIN)
        _app, client = _make_app(db_path, admin1)

        resp = client.post(
            f"/api/admin/users/{admin2.id}/deactivate",
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_delete_admin_when_multiple_admins(self, tmp_path: Path) -> None:
        """Admin can delete another admin when 2+ admins exist."""
        db_path = _make_db(tmp_path)
        admin1 = _make_user(db_path, email="admin1@example.com", role=Role.ADMIN)
        admin2 = _make_user(db_path, email="admin2@example.com", role=Role.ADMIN)
        _app, client = _make_app(db_path, admin1)

        resp = client.request(
            "DELETE",
            f"/api/admin/users/{admin2.id}",
            json={"confirm_email": admin2.email},
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
