"""tests/unit/test_web_metabase_proxy.py

Tests for Metabase session bridging integration in login/logout endpoints
and proxy re-bridge logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.models import Role, User
from dango.auth.security import hash_password
from dango.auth.sessions import create_session
from dango.migrations.runner import MigrationRunner
from dango.web.middleware.auth import COOKIE_NAME
from dango.web.routes.auth import router as auth_router

# Lazy imports in auth.py mean we patch at the origin module.
_BRIDGE_LOGIN = "dango.auth.metabase_bridge.bridge_metabase_login"
_BRIDGE_LOGOUT = "dango.auth.metabase_bridge.bridge_metabase_logout"


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    """Create a fresh auth database at the standard project path."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir(exist_ok=True)
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


def _make_auth_app(db_path: Path) -> FastAPI:
    """Create a FastAPI app with auth routes."""
    app = FastAPI()
    app.state.project_root = db_path.parent.parent
    app.include_router(auth_router)
    return app


# ---------------------------------------------------------------------------
# Login + Metabase bridge
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoginMetabaseBridge:
    """Tests for Metabase session bridging during login."""

    def test_login_bridges_metabase_session(self, tmp_path: Path) -> None:
        """Successful login sets metabase.SESSION cookie when bridge succeeds."""
        db_path = _make_db(tmp_path)
        _make_user(db_path, metabase_user_id=42, metabase_password_enc="encrypted")
        app = _make_auth_app(db_path)
        client = TestClient(app, raise_server_exceptions=False)

        with patch(_BRIDGE_LOGIN, new_callable=AsyncMock, return_value="mb-sess-xyz"):
            resp = client.post(
                "/api/auth/login",
                json={"email": "test@example.com", "password": "securepassword123"},
            )

        assert resp.status_code == 200
        assert COOKIE_NAME in resp.cookies
        assert "metabase.SESSION" in resp.cookies
        assert resp.cookies["metabase.SESSION"] == "mb-sess-xyz"

    def test_login_succeeds_when_metabase_down(self, tmp_path: Path) -> None:
        """Login succeeds even if Metabase bridge fails — no metabase cookie."""
        db_path = _make_db(tmp_path)
        _make_user(db_path)
        app = _make_auth_app(db_path)
        client = TestClient(app, raise_server_exceptions=False)

        with patch(_BRIDGE_LOGIN, new_callable=AsyncMock, return_value=None):
            resp = client.post(
                "/api/auth/login",
                json={"email": "test@example.com", "password": "securepassword123"},
            )

        assert resp.status_code == 200
        assert COOKIE_NAME in resp.cookies
        assert "metabase.SESSION" not in resp.cookies

    def test_login_succeeds_when_no_metabase_account(self, tmp_path: Path) -> None:
        """User without Metabase credentials gets no Metabase cookie."""
        db_path = _make_db(tmp_path)
        _make_user(db_path)  # no metabase_user_id or metabase_password_enc
        app = _make_auth_app(db_path)
        client = TestClient(app, raise_server_exceptions=False)

        # bridge_metabase_login checks user.metabase_password_enc before calling httpx
        resp = client.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": "securepassword123"},
        )

        assert resp.status_code == 200
        assert COOKIE_NAME in resp.cookies


# ---------------------------------------------------------------------------
# Logout + Metabase bridge
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLogoutMetabaseBridge:
    """Tests for Metabase session teardown during logout."""

    def _setup_logged_in(self, tmp_path: Path) -> tuple[TestClient, Path, User]:
        """Return a test client with a user session pre-established."""
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        app = _make_auth_app(db_path)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = user
            request.state.auth_method = "session"
            return await call_next(request)

        raw_token, _sess = create_session(db_path, user.id)
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set(COOKIE_NAME, raw_token)
        return client, db_path, user

    def test_logout_deletes_metabase_session(self, tmp_path: Path) -> None:
        """Logout with metabase.SESSION cookie calls bridge_metabase_logout."""
        client, db_path, user = self._setup_logged_in(tmp_path)
        client.cookies.set("metabase.SESSION", "mb-sess-abc")

        with patch(_BRIDGE_LOGOUT, new_callable=AsyncMock, return_value=True) as mock_logout:
            resp = client.post(
                "/api/auth/logout",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert resp.status_code == 200
        mock_logout.assert_called_once()

    def test_logout_without_metabase_cookie(self, tmp_path: Path) -> None:
        """Logout without metabase.SESSION does not attempt bridge logout."""
        client, db_path, user = self._setup_logged_in(tmp_path)

        with patch(_BRIDGE_LOGOUT, new_callable=AsyncMock) as mock_logout:
            resp = client.post(
                "/api/auth/logout",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert resp.status_code == 200
        mock_logout.assert_not_called()

    def test_logout_succeeds_when_bridge_fails(self, tmp_path: Path) -> None:
        """Logout succeeds even if Metabase session deletion fails."""
        client, db_path, user = self._setup_logged_in(tmp_path)
        client.cookies.set("metabase.SESSION", "mb-sess-abc")

        with patch(_BRIDGE_LOGOUT, new_callable=AsyncMock, side_effect=ConnectionError("refused")):
            resp = client.post(
                "/api/auth/logout",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert resp.status_code == 200
