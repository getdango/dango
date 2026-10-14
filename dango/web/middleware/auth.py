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

Fail-closed rules: an unreadable ``auth.yml`` counts as "auth enabled"
whenever ``auth.db`` exists or the process runs on a cloud server, and that
error result is never cached.  ``scope["state"]["auth_disabled"]`` is True
only when the toggle was read successfully as disabled on a local (non-cloud)
process; ``require_permission()`` relies on it.

WebSocket scopes are authenticated here too (same credentials as HTTP, no
CSRF check), after a same-origin check on the ``Origin`` header.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dango.auth import sessions
from dango.auth.admin import get_auth_db_path, is_auth_enabled
from dango.auth.models import User
from dango.config.helpers import is_running_on_cloud
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
    "/api/internal/scheduler/status",  # CLI/MCP scheduler status (localhost-only check in handler)
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
        # Auth toggle cache (only successful reads are cached)
        self._auth_enabled_cache: bool | None = None
        self._cache_time: float = 0.0
        # True while the most recent toggle read failed (cache hits leave it as is)
        self._toggle_read_failed: bool = False
        # Log a toggle read failure once per failure streak
        self._toggle_error_logged: bool = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point."""
        scope_type = scope.get("type")
        if scope_type == "websocket":
            await self._handle_websocket(scope, receive, send)
            return
        if scope_type != "http":
            # lifespan and any other scope type pass through unchanged
            await self.app(scope, receive, send)
            return

        # Ensure state dict exists
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["auth_disabled"] = False

        # Check auth toggle
        if not self._is_auth_enabled():
            scope["state"]["user"] = None
            scope["state"]["auth_method"] = None
            scope["state"]["auth_disabled"] = self._is_explicitly_disabled()
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
                detail="Auth enabled but auth.db not found",
            )
            if is_running_on_cloud():
                await _make_503_json()(scope, receive, send)
                return
            # Local mode: unchanged (treated as disabled, but never auth_disabled)
            scope["state"]["user"] = None
            scope["state"]["auth_method"] = None
            await self.app(scope, receive, send)
            return

        # Validate credentials (cookie takes precedence)
        headers = scope.get("headers", [])
        user, auth_method = self._authenticate(headers, db_path)

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

    async def _handle_websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate a WebSocket handshake; close with 1008 on failure.

        Order: same-origin check, auth toggle, auth.db presence, then the
        same cookie / API-key validation as HTTP.  No CSRF check (the
        Origin check covers cross-site handshakes).
        """
        headers = scope.get("headers", [])
        if not _is_same_origin(headers):
            logger.warning("ws_cross_origin_refused", path=scope.get("path", ""))
            await _close_websocket(receive, send)
            return

        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["auth_disabled"] = False

        if not self._is_auth_enabled():
            scope["state"]["user"] = None
            scope["state"]["auth_method"] = None
            scope["state"]["auth_disabled"] = self._is_explicitly_disabled()
            await self.app(scope, receive, send)
            return

        db_path = self._get_db_path()
        if not db_path.exists():
            logger.warning(
                "auth_db_missing",
                db_path=str(db_path),
                detail="Auth enabled but auth.db not found",
            )
            if is_running_on_cloud():
                await _close_websocket(receive, send)
                return
            scope["state"]["user"] = None
            scope["state"]["auth_method"] = None
            await self.app(scope, receive, send)
            return

        user, auth_method = self._authenticate(headers, db_path)
        if user is None:
            await _close_websocket(receive, send)
            return

        scope["state"]["user"] = user
        scope["state"]["auth_method"] = auth_method
        await self.app(scope, receive, send)

    def _authenticate(
        self, headers: list[tuple[bytes, bytes]], db_path: Path
    ) -> tuple[User | None, str | None]:
        """Validate session cookie, then API key Bearer token.

        Returns ``(user, auth_method)``; ``(None, None)`` when neither is valid.
        """
        cookie_token = _parse_cookie(headers, COOKIE_NAME)
        if cookie_token is not None:
            user = sessions.validate_session(
                db_path, cookie_token, idle_timeout_minutes=self._idle_timeout_minutes
            )
            if user is not None:
                return user, "session"

        bearer_token = _parse_bearer_token(headers)
        if bearer_token is not None:
            user = sessions.validate_api_key(db_path, bearer_token)
            if user is not None:
                return user, "api_key"

        return None, None

    def _is_auth_enabled(self) -> bool:
        """Check auth toggle with TTL cache; fail closed on a read error.

        A read error is never cached: the next request reads the file again.
        """
        now = time.monotonic()
        if self._auth_enabled_cache is not None and now - self._cache_time < _AUTH_TOGGLE_CACHE_TTL:
            return self._auth_enabled_cache

        try:
            enabled = is_auth_enabled(self.project_root)
        except Exception:
            # Unreadable toggle must never mean "auth off": closed if auth was ever set up.
            self._toggle_read_failed = True
            failing_closed = self._get_db_path().exists() or is_running_on_cloud()
            if not self._toggle_error_logged:
                logger.error(
                    "auth_toggle_read_failed", failing_closed=failing_closed, exc_info=True
                )
                self._toggle_error_logged = True
            return failing_closed

        self._toggle_read_failed = False
        self._toggle_error_logged = False
        self._auth_enabled_cache = enabled
        self._cache_time = now
        return enabled

    def _is_explicitly_disabled(self) -> bool:
        """True only if the toggle was read successfully as off on a local process.

        Call after ``_is_auth_enabled()`` returned False.
        """
        return not self._toggle_read_failed and not is_running_on_cloud()

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


_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_LOOPBACK_ALIASES: frozenset[str] = frozenset({"localhost", "127.0.0.1"})


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    """Return the first value of header *name* (lower-case bytes), decoded."""
    for header_name, header_value in headers:
        if header_name == name:
            return header_value.decode("latin-1")
    return None


def _is_same_origin(headers: list[tuple[bytes, bytes]]) -> bool:
    """Check a WebSocket handshake's ``Origin`` against its ``Host`` header.

    No ``Origin`` header (non-browser client) -> True; credentials are still
    required afterwards.  Otherwise the origin's host and port (default port
    taken from its scheme) must equal the ``Host`` header's, where ``localhost``
    and ``127.0.0.1`` count as the same host.  ``X-Forwarded-Host`` is not trusted.
    """
    origin = _header(headers, b"origin")
    if origin is None:
        return True
    host = _header(headers, b"host")
    if not host:
        return False
    try:
        parsed_origin = urlsplit(origin.strip())
        scheme = parsed_origin.scheme.lower()
        default_port = _DEFAULT_PORTS.get(scheme)
        origin_host = parsed_origin.hostname
        if default_port is None or not origin_host:
            return False  # e.g. "null" or an unknown scheme
        origin_port = parsed_origin.port or default_port
        parsed_host = urlsplit("//" + host.strip())
        host_name = parsed_host.hostname
        if not host_name:
            return False
        host_port = parsed_host.port or default_port
    except ValueError:
        return False  # malformed port

    if origin_port != host_port:
        return False
    if origin_host == host_name:
        return True
    return origin_host in _LOOPBACK_ALIASES and host_name in _LOOPBACK_ALIASES


async def _close_websocket(receive: Receive, send: Send) -> None:
    """Refuse a WebSocket handshake with close code 1008 (policy violation)."""
    await receive()  # consume "websocket.connect"
    await send({"type": "websocket.close", "code": 1008})


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


def _make_503_json() -> Any:
    """Create a 503 JSON ASGI response for a missing auth database (no paths)."""
    from starlette.responses import Response

    body = json.dumps(
        {
            "error_code": "DANGO-S009",
            "message": "Authentication database unavailable",
        }
    )
    return Response(content=body, status_code=503, media_type="application/json")


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
