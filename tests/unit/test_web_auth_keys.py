"""tests/unit/test_web_auth_keys.py

Tests for session management, API key CRUD, and page routes
in dango/web/routes/auth.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.models import Role, User
from dango.auth.security import hash_password
from dango.auth.sessions import create_api_key, create_session
from dango.migrations.runner import MigrationRunner
from dango.web.middleware.auth import COOKIE_NAME
from dango.web.routes.auth import router

# ---------------------------------------------------------------------------
# Test infrastructure (duplicated from test_web_auth.py for independence)
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
    """Create a minimal FastAPI app with auth routes for testing."""
    app = FastAPI()
    # project_root is the parent of .dango/ which contains auth.db
    app.state.project_root = db_path.parent.parent
    app.include_router(router)
    return app


def _make_client(app: FastAPI) -> TestClient:
    """Create a test client."""
    return TestClient(app, raise_server_exceptions=False)


def _auth_headers() -> dict[str, str]:
    """Standard headers for authenticated requests."""
    return {"X-Requested-With": "XMLHttpRequest"}


def _setup_auth_client(
    tmp_path: Path,
    **user_overrides: Any,
) -> tuple[TestClient, Path, User]:
    """Set up a test client with a logged-in user."""
    db_path = _make_db(tmp_path)
    user = _make_user(db_path, **user_overrides)
    app = _make_app(db_path)
    client = _make_client(app)

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    return client, db_path, user


# ---------------------------------------------------------------------------
# Session management tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSessionManagement:
    """Tests for session list and revoke endpoints."""

    def test_list_sessions(self, tmp_path: Path) -> None:
        """Lists user's active sessions with is_current indicator."""
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        app = _make_app(db_path)

        raw_token, _session = create_session(db_path, user.id)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = user
            request.state.auth_method = "session"
            return await call_next(request)

        client = _make_client(app)
        client.cookies.set(COOKIE_NAME, raw_token)

        resp = client.get("/api/auth/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        current_sessions = [s for s in data if s["is_current"]]
        assert len(current_sessions) == 1

    def test_revoke_other_session(self, tmp_path: Path) -> None:
        """Can revoke another session."""
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        app = _make_app(db_path)

        current_token, _current_session = create_session(db_path, user.id)
        _other_token, other_session = create_session(db_path, user.id)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = user
            request.state.auth_method = "session"
            return await call_next(request)

        client = _make_client(app)
        client.cookies.set(COOKIE_NAME, current_token)

        resp = client.delete(f"/api/auth/sessions/{other_session.id}", headers=_auth_headers())
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_revoke_other_users_session(self, tmp_path: Path) -> None:
        """Cannot revoke another user's session (returns 404)."""
        db_path = _make_db(tmp_path)
        user1 = _make_user(db_path, email="user1@example.com")
        user2 = _make_user(db_path, email="user2@example.com")
        app = _make_app(db_path)

        _token2, session2 = create_session(db_path, user2.id)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = user1
            request.state.auth_method = "session"
            return await call_next(request)

        client = _make_client(app)
        resp = client.delete(f"/api/auth/sessions/{session2.id}", headers=_auth_headers())
        assert resp.status_code == 404

    def test_revoke_current_session_rejected(self, tmp_path: Path) -> None:
        """Cannot revoke the current session (must use logout)."""
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        app = _make_app(db_path)

        current_token, current_session = create_session(db_path, user.id)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = user
            request.state.auth_method = "session"
            return await call_next(request)

        client = _make_client(app)
        client.cookies.set(COOKIE_NAME, current_token)

        resp = client.delete(f"/api/auth/sessions/{current_session.id}", headers=_auth_headers())
        assert resp.status_code == 400
        assert "logout" in resp.json()["message"].lower()


# ---------------------------------------------------------------------------
# Unauthenticated access tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUnauthenticatedAccess:
    """Protected endpoints return 401 when no user is set."""

    def _make_unauth_client(self, tmp_path: Path) -> TestClient:
        """Create a client with no user in request state."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = None
            request.state.auth_method = None
            return await call_next(request)

        return _make_client(app)

    def test_sessions_requires_auth(self, tmp_path: Path) -> None:
        """GET /api/auth/sessions returns 401 without auth."""
        client = self._make_unauth_client(tmp_path)
        assert client.get("/api/auth/sessions").status_code == 401

    def test_api_keys_requires_auth(self, tmp_path: Path) -> None:
        """GET /api/auth/api-keys returns 401 without auth."""
        client = self._make_unauth_client(tmp_path)
        assert client.get("/api/auth/api-keys").status_code == 401

    def test_change_password_requires_auth(self, tmp_path: Path) -> None:
        """POST /api/auth/change-password returns 401 without auth."""
        client = self._make_unauth_client(tmp_path)
        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "x", "new_password": "y"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# API key management tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApiKeyManagement:
    """Tests for API key CRUD endpoints."""

    def test_create_api_key(self, tmp_path: Path) -> None:
        """Creating an API key returns the full key once."""
        client, _db_path, _user = _setup_auth_client(tmp_path)

        resp = client.post(
            "/api/auth/api-keys",
            json={"name": "test-key"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-key"
        assert data["key"].startswith("dango_ak_")
        assert len(data["key_prefix"]) > 0

    def test_list_api_keys(self, tmp_path: Path) -> None:
        """Listing API keys returns prefix only, not full key."""
        client, db_path, user = _setup_auth_client(tmp_path)

        _raw_key, _api_key = create_api_key(db_path, user.id, "test-key")

        resp = client.get("/api/auth/api-keys")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "test-key"
        assert "key" not in data[0]

    def test_revoke_api_key(self, tmp_path: Path) -> None:
        """Revoking an API key succeeds for owned keys."""
        client, db_path, user = _setup_auth_client(tmp_path)

        _raw_key, api_key = create_api_key(db_path, user.id, "test-key")

        resp = client.delete(f"/api/auth/api-keys/{api_key.id}", headers=_auth_headers())
        assert resp.status_code == 200
        assert resp.json()["success"] is True


# ---------------------------------------------------------------------------
# Page route tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPageRoutes:
    """Tests for login and setup page rendering."""

    def test_login_page(self, tmp_path: Path) -> None:
        """Login page renders successfully."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = None
            request.state.auth_method = None
            return await call_next(request)

        client = _make_client(app)
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "Sign in" in resp.text

    def test_setup_page(self, tmp_path: Path) -> None:
        """Setup/change-password page renders successfully."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = None
            request.state.auth_method = None
            return await call_next(request)

        client = _make_client(app)
        resp = client.get("/setup")
        assert resp.status_code == 200
        assert "Change Password" in resp.text
