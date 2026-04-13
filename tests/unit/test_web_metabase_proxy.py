"""tests/unit/test_web_metabase_proxy.py

Tests for Metabase session bridging integration in login/logout endpoints
and proxy re-bridge logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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
from dango.web.routes.metabase_proxy import router as proxy_router

# Lazy imports in auth.py mean we patch at the origin module.
_BRIDGE_LOGIN = "dango.auth.metabase_bridge.bridge_metabase_login"
_BRIDGE_LOGOUT = "dango.auth.metabase_bridge.bridge_metabase_logout"
# Proxy internals — patched directly on the module since they are not lazy-imported.
_DO_PROXY = "dango.web.routes.metabase_proxy._do_proxy"
_REBRIDGE = "dango.web.routes.metabase_proxy._rebridge_if_needed"
_GET_MB_URL = "dango.web.routes.metabase_proxy._get_metabase_url"


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
        assert "metabase.SESSION" not in resp.cookies


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


# ---------------------------------------------------------------------------
# Proxy re-bridge on 401
# ---------------------------------------------------------------------------


def _httpx_response(status: int = 200, body: bytes = b"ok") -> httpx.Response:
    """Build a minimal httpx.Response for proxy mock returns."""
    return httpx.Response(status_code=status, content=body)


def _make_proxy_app(tmp_path: Path) -> FastAPI:
    """Create a FastAPI app with proxy routes and a fake authenticated user."""
    app = FastAPI()
    app.state.project_root = tmp_path

    user = MagicMock()
    user.email = "proxy@example.com"

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    app.include_router(proxy_router)
    return app


@pytest.mark.unit
class TestProxyRebridge:
    """Tests for the re-bridge-on-401 pattern in the Metabase proxy."""

    def test_401_rebridge_retry_succeeds(self, tmp_path: Path) -> None:
        """On 401, proxy re-bridges and retries; new cookie is set."""
        app = _make_proxy_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("metabase.SESSION", "old-sess")

        first_resp = _httpx_response(401)
        retry_resp = _httpx_response(200, b"dashboard data")

        with (
            patch(_DO_PROXY, new_callable=AsyncMock, side_effect=[first_resp, retry_resp]),
            patch(_REBRIDGE, new_callable=AsyncMock, return_value="new-sess-id"),
            patch(_GET_MB_URL, return_value="http://mb:3000"),
        ):
            resp = client.get("/metabase/api/card")

        assert resp.status_code == 200
        assert resp.text == "dashboard data"
        assert "metabase.SESSION" in resp.cookies
        assert resp.cookies["metabase.SESSION"] == "new-sess-id"

    def test_401_rebridge_fails_returns_401(self, tmp_path: Path) -> None:
        """On 401, if re-bridge fails, the original 401 is returned."""
        app = _make_proxy_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("metabase.SESSION", "old-sess")

        first_resp = _httpx_response(401, b"Unauthorized")

        with (
            patch(_DO_PROXY, new_callable=AsyncMock, return_value=first_resp),
            patch(_REBRIDGE, new_callable=AsyncMock, return_value=None),
            patch(_GET_MB_URL, return_value="http://mb:3000"),
        ):
            resp = client.get("/metabase/api/card")

        assert resp.status_code == 401
        assert "metabase.SESSION" not in resp.cookies

    def test_200_no_rebridge(self, tmp_path: Path) -> None:
        """On 200, no re-bridge is attempted."""
        app = _make_proxy_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("metabase.SESSION", "valid-sess")

        ok_resp = _httpx_response(200, b"ok")

        with (
            patch(_DO_PROXY, new_callable=AsyncMock, return_value=ok_resp) as mock_proxy,
            patch(_REBRIDGE, new_callable=AsyncMock) as mock_rebridge,
            patch(_GET_MB_URL, return_value="http://mb:3000"),
        ):
            resp = client.get("/metabase/api/card")

        assert resp.status_code == 200
        mock_proxy.assert_called_once()
        mock_rebridge.assert_not_called()


# ---------------------------------------------------------------------------
# Auto-bridge on missing session (BUG-011)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutobridge:
    """Tests for auto-bridging when user has no metabase.SESSION cookie."""

    def test_auto_bridge_on_missing_session(self, tmp_path: Path) -> None:
        """Authenticated user without cookie gets auto-bridged session."""
        app = _make_proxy_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        # No metabase.SESSION cookie set

        ok_resp = _httpx_response(200, b"dashboard html")

        with (
            patch(_DO_PROXY, new_callable=AsyncMock, return_value=ok_resp),
            patch(_REBRIDGE, new_callable=AsyncMock, return_value="auto-sess-new"),
            patch(_GET_MB_URL, return_value="http://mb:3000"),
        ):
            resp = client.get("/metabase/")

        assert resp.status_code == 200
        assert resp.text == "dashboard html"
        assert "metabase.SESSION" in resp.cookies
        assert resp.cookies["metabase.SESSION"] == "auto-sess-new"

    def test_auto_bridge_failure_passes_through(self, tmp_path: Path) -> None:
        """If auto-bridge fails, request is proxied without session (Metabase login page)."""
        app = _make_proxy_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)

        login_page = _httpx_response(200, b"<html>Metabase Login</html>")

        with (
            patch(_DO_PROXY, new_callable=AsyncMock, return_value=login_page),
            patch(_REBRIDGE, new_callable=AsyncMock, return_value=None),
            patch(_GET_MB_URL, return_value="http://mb:3000"),
        ):
            resp = client.get("/metabase/")

        assert resp.status_code == 200
        assert b"Metabase Login" in resp.content
        assert "metabase.SESSION" not in resp.cookies


# ---------------------------------------------------------------------------
# 403 re-bridge (BUG-015)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProxy403Rebridge:
    """Tests for re-bridging on 403 responses from Metabase."""

    def test_403_rebridge_retry_succeeds(self, tmp_path: Path) -> None:
        """On 403, proxy re-bridges and retries; new cookie is set."""
        app = _make_proxy_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("metabase.SESSION", "stale-sess")

        first_resp = _httpx_response(403, b"Forbidden")
        retry_resp = _httpx_response(200, b"dashboard data")

        with (
            patch(_DO_PROXY, new_callable=AsyncMock, side_effect=[first_resp, retry_resp]),
            patch(_REBRIDGE, new_callable=AsyncMock, return_value="fresh-sess"),
            patch(_GET_MB_URL, return_value="http://mb:3000"),
        ):
            resp = client.get("/metabase/api/card")

        assert resp.status_code == 200
        assert resp.text == "dashboard data"
        assert "metabase.SESSION" in resp.cookies
        assert resp.cookies["metabase.SESSION"] == "fresh-sess"

    def test_403_rebridge_fails_returns_403(self, tmp_path: Path) -> None:
        """On 403, if re-bridge fails, original 403 is returned."""
        app = _make_proxy_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("metabase.SESSION", "stale-sess")

        first_resp = _httpx_response(403, b"Forbidden")

        with (
            patch(_DO_PROXY, new_callable=AsyncMock, return_value=first_resp),
            patch(_REBRIDGE, new_callable=AsyncMock, return_value=None),
            patch(_GET_MB_URL, return_value="http://mb:3000"),
        ):
            resp = client.get("/metabase/api/card")

        assert resp.status_code == 403
        assert "metabase.SESSION" not in resp.cookies


# ---------------------------------------------------------------------------
# _build_response multi-header preservation (BUG-014)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildResponse:
    """Tests for _build_response preserving multi-valued headers."""

    def test_preserves_multi_set_cookie(self) -> None:
        """Multiple Set-Cookie headers from Metabase are all forwarded."""
        from dango.web.routes.metabase_proxy import _build_response

        raw_response = httpx.Response(
            status_code=200,
            content=b"ok",
            headers=[
                ("content-type", "text/html"),
                ("set-cookie", "cookie1=val1; Path=/"),
                ("set-cookie", "cookie2=val2; Path=/; HttpOnly"),
            ],
        )
        result = _build_response(raw_response)

        assert result.status_code == 200
        # Collect all set-cookie headers from the response
        set_cookies = [v for k, v in result.headers.items() if k.lower() == "set-cookie"]
        assert len(set_cookies) == 2
        assert "cookie1=val1" in set_cookies[0]
        assert "cookie2=val2" in set_cookies[1]
