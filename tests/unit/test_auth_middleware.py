"""tests/unit/test_auth_middleware.py

Tests for the authentication ASGI middleware in dango/web/middleware/auth.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.auth.models import Role, User
from dango.web.middleware.auth import COOKIE_NAME, AuthMiddleware

# ---------------------------------------------------------------------------
# ASGI test helpers (matches test_rate_limit.py pattern)
# ---------------------------------------------------------------------------

_captured_state: dict[str, Any] = {}


async def _noop_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Minimal ASGI app that captures state and returns 200 OK."""
    _captured_state.clear()
    _captured_state.update(scope.get("state", {}))
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"OK"})


def _make_scope(
    path: str = "/api/sources",
    method: str = "GET",
    scope_type: str = "http",
    headers: list[tuple[bytes, bytes]] | None = None,
    cookie: str | None = None,
    bearer: str | None = None,
) -> dict[str, Any]:
    """Create a minimal ASGI HTTP scope dict with optional auth headers."""
    raw_headers: list[tuple[bytes, bytes]] = list(headers or [])
    if cookie is not None:
        raw_headers.append((b"cookie", f"{COOKIE_NAME}={cookie}".encode()))
    if bearer is not None:
        raw_headers.append((b"authorization", f"Bearer {bearer}".encode()))
    return {
        "type": scope_type,
        "method": method,
        "path": path,
        "headers": raw_headers,
    }


async def _collect_response(
    middleware: AuthMiddleware,
    scope: dict[str, Any],
) -> dict[str, Any]:
    """Run middleware and collect status, headers, and body."""
    result: dict[str, Any] = {"status": 0, "headers": {}, "body": b""}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
            for name, value in message.get("headers", []):
                key = name.decode() if isinstance(name, bytes) else name
                val = value.decode() if isinstance(value, bytes) else value
                result["headers"][key] = val
        elif message["type"] == "http.response.body":
            result["body"] += message.get("body", b"")

    await middleware(scope, receive, send)
    return result


def _make_user(email: str = "test@example.com", role: Role = Role.EDITOR) -> User:
    """Create a minimal User for testing."""
    return User(email=email, role=role, password_hash="hashed")


_PROJECT_ROOT = Path("/tmp/dango-test-project")
_M = "dango.web.middleware.auth"


def _db_exists() -> MagicMock:
    """Return a fresh mock Path whose .exists() returns True."""
    return MagicMock(exists=MagicMock(return_value=True))


# ---------------------------------------------------------------------------
# Auth disabled / public routes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuthDisabled:
    """When auth is disabled, all requests pass through with user=None."""

    @patch(f"{_M}.is_auth_enabled", return_value=False)
    def test_passes_through(self, _m: Any) -> None:
        """Auth disabled: user is None, request reaches inner app."""
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        result = asyncio.run(_collect_response(mw, _make_scope()))
        assert result["status"] == 200
        assert _captured_state["user"] is None

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    def test_missing_db(self, mock_db: Any, _m: Any) -> None:
        """Auth enabled but auth.db missing: treated as disabled."""
        mock_db.return_value = Path("/nonexistent/auth.db")
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        result = asyncio.run(_collect_response(mw, _make_scope()))
        assert result["status"] == 200
        assert _captured_state["user"] is None


@pytest.mark.unit
class TestPublicRoutes:
    """Public routes bypass auth even when auth is enabled."""

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    def test_exact_public_routes(self, _m: Any) -> None:
        """Each exact public route returns 200 with user=None."""
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        for path in [
            "/api/auth/login",
            "/login",
            "/setup",
            "/api/health",
            "/favicon.ico",
            "/api/auth/oauth/callback",
        ]:
            result = asyncio.run(_collect_response(mw, _make_scope(path=path)))
            assert result["status"] == 200, f"{path} was blocked"
            assert _captured_state["user"] is None

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    def test_prefix_public_routes(self, _m: Any) -> None:
        """Prefix-matched public routes pass through."""
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        for path in ["/api/auth/oauth/google", "/static/css/main.css"]:
            result = asyncio.run(_collect_response(mw, _make_scope(path=path)))
            assert result["status"] == 200, f"{path} was blocked"

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session", return_value=None)
    def test_page_routes_require_auth(self, _v: Any, mock_db: Any, _m: Any) -> None:
        """Page routes (/, /health, /logs) redirect to /login when unauthenticated."""
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        for path in ["/", "/health", "/logs"]:
            scope = _make_scope(
                path=path,
                headers=[(b"accept", b"text/html,application/xhtml+xml")],
            )
            result = asyncio.run(_collect_response(mw, scope))
            assert result["status"] == 302, f"{path} did not redirect"
            assert result["headers"].get("location") == "/login", f"{path} wrong redirect"

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session", return_value=None)
    def test_api_docs_require_auth(self, _v: Any, mock_db: Any, _m: Any) -> None:
        """API doc routes (/api, /api/docs, /api/redoc) return 401 for API clients."""
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        for path in ["/api", "/api/docs", "/api/redoc"]:
            result = asyncio.run(_collect_response(mw, _make_scope(path=path)))
            assert result["status"] == 401, f"{path} did not return 401"


# ---------------------------------------------------------------------------
# Session cookie auth
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSessionCookieAuth:
    """Authentication via session cookie."""

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session")
    def test_valid_cookie(self, mock_val: Any, mock_db: Any, _m: Any) -> None:
        """Valid session cookie sets user and auth_method=session."""
        user = _make_user()
        mock_val.return_value = user
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        result = asyncio.run(_collect_response(mw, _make_scope(cookie="tok")))
        assert result["status"] == 200
        assert _captured_state["user"] is user
        assert _captured_state["auth_method"] == "session"

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session", return_value=None)
    def test_invalid_cookie_401(self, _v: Any, mock_db: Any, _m: Any) -> None:
        """Invalid session cookie returns 401 JSON."""
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        result = asyncio.run(_collect_response(mw, _make_scope(cookie="bad")))
        assert result["status"] == 401
        assert json.loads(result["body"])["error_code"] == "DANGO-S001"

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session", return_value=None)
    def test_invalid_cookie_browser_redirect(self, _v: Any, mock_db: Any, _m: Any) -> None:
        """Invalid cookie + browser Accept: 302 redirect to /login."""
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        scope = _make_scope(cookie="bad", headers=[(b"accept", b"text/html,application/xhtml+xml")])
        result = asyncio.run(_collect_response(mw, scope))
        assert result["status"] == 302
        assert "/login" in result["headers"].get("location", "")


# ---------------------------------------------------------------------------
# API key auth
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApiKeyAuth:
    """Authentication via Bearer API key."""

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_api_key")
    def test_valid_bearer(self, mock_val: Any, mock_db: Any, _m: Any) -> None:
        """Valid Bearer token sets user and auth_method=api_key."""
        user = _make_user()
        mock_val.return_value = user
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        result = asyncio.run(_collect_response(mw, _make_scope(bearer="dango_ak_ok")))
        assert result["status"] == 200
        assert _captured_state["user"] is user
        assert _captured_state["auth_method"] == "api_key"

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_api_key", return_value=None)
    def test_invalid_bearer_401(self, _v: Any, mock_db: Any, _m: Any) -> None:
        """Invalid Bearer token returns 401."""
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        result = asyncio.run(_collect_response(mw, _make_scope(bearer="bad")))
        assert result["status"] == 401

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    def test_no_credentials_401(self, mock_db: Any, _m: Any) -> None:
        """No cookie or Bearer token returns 401."""
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        result = asyncio.run(_collect_response(mw, _make_scope()))
        assert result["status"] == 401


# ---------------------------------------------------------------------------
# CSRF protection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCsrfProtection:
    """CSRF protection for cookie-authenticated state-changing requests."""

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session")
    def test_post_without_csrf_403(self, mock_val: Any, mock_db: Any, _m: Any) -> None:
        """Cookie auth + POST without CSRF header: 403."""
        mock_val.return_value = _make_user()
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        scope = _make_scope(path="/api/sources", method="POST", cookie="v")
        result = asyncio.run(_collect_response(mw, scope))
        assert result["status"] == 403
        body = json.loads(result["body"])
        assert body["error_code"] == "DANGO-S002"

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session")
    def test_delete_without_csrf_403(self, mock_val: Any, mock_db: Any, _m: Any) -> None:
        """Cookie auth + DELETE without CSRF header: 403."""
        mock_val.return_value = _make_user()
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        scope = _make_scope(path="/api/sources/x", method="DELETE", cookie="v")
        result = asyncio.run(_collect_response(mw, scope))
        assert result["status"] == 403

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session")
    def test_post_with_x_requested_with(self, mock_val: Any, mock_db: Any, _m: Any) -> None:
        """Cookie auth + POST + X-Requested-With: passes."""
        mock_val.return_value = _make_user()
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        scope = _make_scope(
            method="POST", cookie="v", headers=[(b"x-requested-with", b"XMLHttpRequest")]
        )
        assert asyncio.run(_collect_response(mw, scope))["status"] == 200

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session")
    def test_post_with_csrf_protection(self, mock_val: Any, mock_db: Any, _m: Any) -> None:
        """Cookie auth + POST + X-CSRF-Protection: passes."""
        mock_val.return_value = _make_user()
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        scope = _make_scope(method="POST", cookie="v", headers=[(b"x-csrf-protection", b"1")])
        assert asyncio.run(_collect_response(mw, scope))["status"] == 200

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_api_key")
    def test_api_key_post_no_csrf(self, mock_val: Any, mock_db: Any, _m: Any) -> None:
        """API key auth + POST without CSRF: passes (API keys skip CSRF)."""
        mock_val.return_value = _make_user()
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        scope = _make_scope(method="POST", bearer="dango_ak_x")
        assert asyncio.run(_collect_response(mw, scope))["status"] == 200

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session")
    def test_get_no_csrf_needed(self, mock_val: Any, mock_db: Any, _m: Any) -> None:
        """Cookie auth + GET: no CSRF check (safe method)."""
        mock_val.return_value = _make_user()
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        assert asyncio.run(_collect_response(mw, _make_scope(cookie="v")))["status"] == 200

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session")
    def test_metabase_proxy_post_exempt_from_csrf(
        self, mock_val: Any, mock_db: Any, _m: Any
    ) -> None:
        """Cookie auth + POST to /metabase/... without CSRF header: passes (exempt)."""
        mock_val.return_value = _make_user()
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        scope = _make_scope(path="/metabase/api/card", method="POST", cookie="v")
        result = asyncio.run(_collect_response(mw, scope))
        assert result["status"] == 200

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session")
    def test_non_metabase_path_still_requires_csrf(
        self, mock_val: Any, mock_db: Any, _m: Any
    ) -> None:
        """POST to /metabase_other/... (similar prefix) still requires CSRF."""
        mock_val.return_value = _make_user()
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        scope = _make_scope(path="/metabase_other/api", method="POST", cookie="v")
        result = asyncio.run(_collect_response(mw, scope))
        assert result["status"] == 403


# ---------------------------------------------------------------------------
# Auth precedence
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuthPrecedence:
    """Cookie takes precedence over Bearer when both are present."""

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_api_key")
    @patch(f"{_M}.sessions.validate_session")
    def test_cookie_wins(self, m_ses: Any, m_ak: Any, mock_db: Any, _m: Any) -> None:
        """Valid cookie + valid Bearer: cookie user is used."""
        m_ses.return_value = _make_user(email="cookie@x.com")
        m_ak.return_value = _make_user(email="bearer@x.com")
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        scope = _make_scope(cookie="s", bearer="dango_ak_x")
        asyncio.run(_collect_response(mw, scope))
        assert _captured_state["user"].email == "cookie@x.com"
        assert _captured_state["auth_method"] == "session"

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_api_key")
    @patch(f"{_M}.sessions.validate_session", return_value=None)
    def test_fallthrough_to_bearer(self, _s: Any, m_ak: Any, mock_db: Any, _m: Any) -> None:
        """Invalid cookie + valid Bearer: falls through to Bearer."""
        m_ak.return_value = _make_user(email="bearer@x.com")
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        scope = _make_scope(cookie="bad", bearer="dango_ak_ok")
        asyncio.run(_collect_response(mw, scope))
        assert _captured_state["user"].email == "bearer@x.com"
        assert _captured_state["auth_method"] == "api_key"


# ---------------------------------------------------------------------------
# Non-HTTP scope passthrough
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNonHttpPassthrough:
    """Non-HTTP scopes (WebSocket, lifespan) bypass auth entirely."""

    def test_websocket(self) -> None:
        """WebSocket scope passes through."""
        received = False

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            nonlocal received
            received = True

        mw = AuthMiddleware(app, project_root=_PROJECT_ROOT)
        asyncio.run(mw({"type": "websocket"}, None, None))
        assert received

    def test_lifespan(self) -> None:
        """Lifespan scope passes through."""
        received = False

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            nonlocal received
            received = True

        mw = AuthMiddleware(app, project_root=_PROJECT_ROOT)
        asyncio.run(mw({"type": "lifespan"}, None, None))
        assert received


# ---------------------------------------------------------------------------
# Auth toggle cache
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuthToggleCache:
    """Auth toggle is cached for TTL period."""

    @patch(f"{_M}.is_auth_enabled", return_value=True)
    @patch(f"{_M}.get_auth_db_path")
    @patch(f"{_M}.sessions.validate_session")
    def test_cache_avoids_repeated_reads(self, m_val: Any, mock_db: Any, m_en: Any) -> None:
        """Two requests within TTL call is_auth_enabled only once."""
        m_val.return_value = _make_user()
        mock_db.return_value = _db_exists()
        mw = AuthMiddleware(_noop_app, project_root=_PROJECT_ROOT)
        scope = _make_scope(cookie="v")
        asyncio.run(_collect_response(mw, scope))
        asyncio.run(_collect_response(mw, scope))
        assert m_en.call_count == 1
