"""dango/web/middleware/auth.py

Pure ASGI middleware for request authentication.

Extracts session cookies or API key Bearer tokens from incoming requests,
validates them against the auth database, enforces CSRF protection for
cookie-authenticated state-changing requests, and populates
``scope["state"]["user"]`` for downstream route handlers and the
``require_permission()`` dependency.

Auth can be toggled via ``.dango/auth.yml``; when disabled, all requests
pass through with ``user=None``.  Public routes (login page, health
endpoints, static assets) always bypass authentication.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dango.auth import sessions
from dango.auth.admin import get_auth_db_path, is_auth_enabled
from dango.logging import get_logger

logger = get_logger(__name__)

# ASGI type aliases (matches rate_limit.py)
Scope = dict[str, Any]
Receive = Any  # ASGI receive callable
Send = Any  # ASGI send callable

# ---------------------------------------------------------------------------
# Cookie / session constants
# ---------------------------------------------------------------------------

COOKIE_NAME: str = "dango_session"
"""Name of the session cookie set by the login endpoint."""

# ---------------------------------------------------------------------------
# Auth toggle cache — avoids filesystem read on every request
# ---------------------------------------------------------------------------

_AUTH_TOGGLE_CACHE_TTL: float = 5.0  # seconds

# ---------------------------------------------------------------------------
# Public routes — never require authentication
# ---------------------------------------------------------------------------

# NOTE: /api/status, /api/watcher/status, /api/health/platform are
# intentionally NOT public — they expose detailed system information.
# Only /api/health (basic load-balancer probe) is unauthenticated.
# Similarly, /dbt-docs/ and its JSON assets (/manifest.json,
# /catalog.json) require auth — dbt model metadata is project data.

# fmt: off
_PUBLIC_EXACT: frozenset[str] = frozenset({
    "/api/auth/login",
    "/api/auth/2fa/verify",
    "/api/auth/oauth/callback",
    "/api/auth/accept-invite",
    "/api/initial-sync/start",  # Accepts deploy token OR admin session (own auth check)
    "/api/internal/schedules/reload",  # CLI schedule reload (localhost-only check in handler)
    "/login",
    "/setup",
    "/api/health",
    "/favicon.ico",
})
# fmt: on

_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/api/auth/oauth/",
    "/invite/",
    "/static/",
)

# Methods that do NOT require CSRF protection
_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Localhost addresses (for is_secure_request)
_LOCALHOST_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
    }
)


class AuthMiddleware:
    """ASGI middleware that authenticates requests via session cookies or API keys."""

    def __init__(self, app: Any, project_root: Path, idle_timeout_minutes: int = 60) -> None:
        self.app = app
        self.project_root = project_root
        self._idle_timeout_minutes = idle_timeout_minutes
        # Auth toggle cache
        self._auth_enabled_cache: bool | None = None
        self._cache_time: float = 0.0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point."""
        # Only handle HTTP requests
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Ensure state dict exists
        if "state" not in scope:
            scope["state"] = {}

        # Check auth toggle
        if not self._is_auth_enabled():
            scope["state"]["user"] = None
            scope["state"]["auth_method"] = None
            await self.app(scope, receive, send)
            return

        # Check public routes
        path: str = scope.get("path", "")
        if self._is_public_route(path):
            scope["state"]["user"] = None
            scope["state"]["auth_method"] = None
            await self.app(scope, receive, send)
            return

        # Resolve auth DB path
        db_path = self._get_db_path()
        if not db_path.exists():
            logger.warning(
                "auth_db_missing",
                db_path=str(db_path),
                detail="Auth enabled but auth.db not found; treating as disabled",
            )
            scope["state"]["user"] = None
            scope["state"]["auth_method"] = None
            await self.app(scope, receive, send)
            return

        # Extract credentials
        headers = scope.get("headers", [])
        cookie_token = _parse_cookie(headers, COOKIE_NAME)
        bearer_token = _parse_bearer_token(headers)

        # Validate credentials (cookie takes precedence)
        user = None
        auth_method: str | None = None

        if cookie_token is not None:
            user = sessions.validate_session(
                db_path, cookie_token, idle_timeout_minutes=self._idle_timeout_minutes
            )
            if user is not None:
                auth_method = "session"

        if user is None and bearer_token is not None:
            user = sessions.validate_api_key(db_path, bearer_token)
            if user is not None:
                auth_method = "api_key"

        # No valid credentials
        if user is None:
            if _is_browser_request(headers):
                response = _make_redirect_response("/login")
            else:
                response = _make_401_json()
            await response(scope, receive, send)
            return

        # CSRF check (cookie-auth only, state-changing methods)
        # Metabase and Marimo proxy routes are exempt: their frontends send
        # POSTs without Dango CSRF headers, and both have their own CSRF/token
        # protection.  Dango auth is still required (not a public route).
        method: str = scope.get("method", "GET")
        if auth_method == "session" and method not in _SAFE_METHODS:
            csrf_exempt = path.startswith("/metabase/") or path.startswith("/notebooks/marimo/")
            if not csrf_exempt and not _has_csrf_header(headers):
                response = _make_403_json("CSRF validation failed")
                await response(scope, receive, send)
                return

        # Set user on request state
        scope["state"]["user"] = user
        scope["state"]["auth_method"] = auth_method

        await self.app(scope, receive, send)

    def _is_auth_enabled(self) -> bool:
        """Check auth toggle with TTL cache."""
        now = time.monotonic()
        if self._auth_enabled_cache is not None and now - self._cache_time < _AUTH_TOGGLE_CACHE_TTL:
            return self._auth_enabled_cache

        try:
            enabled = is_auth_enabled(self.project_root)
        except Exception:
            logger.debug("auth_toggle_read_failed", exc_info=True)
            enabled = False

        self._auth_enabled_cache = enabled
        self._cache_time = now
        return enabled

    def _is_public_route(self, path: str) -> bool:
        """Check if a path is a public route that bypasses auth."""
        if path in _PUBLIC_EXACT:
            return True
        for prefix in _PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return True
        return False

    def _get_db_path(self) -> Path:
        """Return path to auth.db."""
        return get_auth_db_path(self.project_root)


# ---------------------------------------------------------------------------
# Header parsing helpers (module-level for testability)
# ---------------------------------------------------------------------------


def _parse_cookie(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    """Extract a named cookie value from ASGI raw headers."""
    target = name.encode("ascii")
    for header_name, header_value in headers:
        if header_name == b"cookie":
            # Cookie header format: "name1=val1; name2=val2"
            for pair in header_value.split(b"; "):
                eq_idx = pair.find(b"=")
                if eq_idx == -1:
                    continue
                if pair[:eq_idx].strip() == target:
                    return pair[eq_idx + 1 :].strip().decode("utf-8", errors="replace")
    return None


def _parse_bearer_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Extract Bearer token from Authorization header."""
    for header_name, header_value in headers:
        if header_name == b"authorization":
            value = header_value.decode("utf-8", errors="replace")
            if value.startswith("Bearer "):
                token = value[7:].strip()
                if token:
                    return token
    return None


def _is_browser_request(headers: list[tuple[bytes, bytes]]) -> bool:
    """Heuristic: request is from a browser if Accept contains text/html."""
    for header_name, header_value in headers:
        if header_name == b"accept":
            return b"text/html" in header_value
    return False


def _has_csrf_header(headers: list[tuple[bytes, bytes]]) -> bool:
    """Check for CSRF protection header (X-Requested-With or X-CSRF-Protection)."""
    for header_name, _header_value in headers:
        if header_name in (b"x-requested-with", b"x-csrf-protection"):
            return True
    return False


# ---------------------------------------------------------------------------
# ASGI response helpers
# ---------------------------------------------------------------------------


def _make_401_json() -> Any:
    """Create a 401 Unauthorized JSON ASGI response."""
    from starlette.responses import Response

    body = json.dumps(
        {
            "error_code": "DANGO-S001",
            "message": "Authentication required.",
        }
    )
    return Response(content=body, status_code=401, media_type="application/json")


def _make_redirect_response(location: str) -> Any:
    """Create a 302 redirect ASGI response."""
    from starlette.responses import RedirectResponse

    return RedirectResponse(url=location, status_code=302)


def _make_403_json(message: str) -> Any:
    """Create a 403 Forbidden JSON ASGI response."""
    from starlette.responses import Response

    body = json.dumps(
        {
            "error_code": "DANGO-S002",
            "message": message,
        }
    )
    return Response(content=body, status_code=403, media_type="application/json")


# ---------------------------------------------------------------------------
# Utility for downstream tasks (TASK-016 cookie settings)
# ---------------------------------------------------------------------------


def is_secure_request(scope: Mapping[str, Any]) -> bool:
    """Detect if request should use Secure cookies.

    Returns ``True`` if the connection is HTTPS or if the host is not
    localhost / 127.0.0.1 (i.e. likely behind a reverse proxy with TLS).
    """
    # HTTPS scheme
    if scope.get("scheme") == "https":
        return True
    # Check host — non-localhost implies production / proxy
    server: tuple[str, int] | None = scope.get("server")
    if server is not None:
        host = server[0]
        if host not in _LOCALHOST_HOSTS:
            return True
    return False
