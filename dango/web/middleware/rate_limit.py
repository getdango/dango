"""dango/web/middleware/rate_limit.py

Pure ASGI middleware for per-IP sliding-window rate limiting.

Uses ``BaseHTTPMiddleware``-free design to avoid issues with WebSocket and
streaming responses. Route classification maps paths to rate limit groups
(``login`` or ``api``), each with independent limits. Localhost traffic and
OPTIONS preflight requests are always allowed.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

from dango.config.models import RateLimitConfig

# ASGI type aliases
Scope = dict[str, Any]
Receive = Any  # ASGI receive callable
Send = Any  # ASGI send callable

_LOCALHOST_IPS: frozenset[str] = frozenset({"127.0.0.1", "::1"})

# Paths that are never rate-limited
_UNLIMITED_PREFIXES: tuple[str, ...] = (
    "/api/status",
    "/api/watcher/status",
    "/api/health",
    "/static/",
    "/dbt-docs/",
)

_UNLIMITED_EXACT: frozenset[str] = frozenset(
    {
        "/",
        "/health",
        "/logs",
        "/ws",
        "/api",
        "/api/docs",
        "/api/redoc",
    }
)

# Cleanup interval in seconds (remove stale IP entries)
_CLEANUP_INTERVAL: float = 300.0


class RateLimitMiddleware:
    """ASGI middleware implementing per-IP sliding-window rate limiting."""

    def __init__(self, app: Any, config: RateLimitConfig | None = None) -> None:
        self.app = app
        self.config = config if config is not None else RateLimitConfig()
        # group -> IP -> deque of timestamps (monotonic seconds)
        self._windows: dict[str, dict[str, deque[float]]] = {}
        self._last_cleanup: float = time.monotonic()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point."""
        # Only rate-limit HTTP requests
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Skip if rate limiting is disabled
        if not self.config.enabled:
            await self.app(scope, receive, send)
            return

        # Skip localhost
        client_ip = self._get_client_ip(scope)
        if client_ip in _LOCALHOST_IPS:
            await self.app(scope, receive, send)
            return

        # Skip OPTIONS (CORS preflight)
        method: str = scope.get("method", "")
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # Classify route
        path: str = scope.get("path", "")
        group = self._classify_route(path)
        if group is None:
            await self.app(scope, receive, send)
            return

        # Check rate limit
        allowed, retry_after = self._check_rate_limit(client_ip, group)
        if not allowed:
            response = self._make_429_response(retry_after)
            await response(scope, receive, send)
            return

        # Periodic cleanup
        self._maybe_cleanup()

        await self.app(scope, receive, send)

    def _get_client_ip(self, scope: Scope) -> str:
        """Extract client IP address from the ASGI scope."""
        client: tuple[str, int] | None = scope.get("client")
        if client is not None:
            return client[0]
        return ""

    def _classify_route(self, path: str) -> str | None:
        """Map a request path to a rate limit group name, or None if unlimited."""
        # Login endpoint gets stricter limits
        if path == "/api/auth/login":
            return "login"

        # Check unlimited exact matches
        if path in _UNLIMITED_EXACT:
            return None

        # Check unlimited prefixes
        for prefix in _UNLIMITED_PREFIXES:
            if path.startswith(prefix):
                return None

        # API and metabase proxy routes
        if path.startswith("/api/") or path.startswith("/metabase/"):
            return "api"

        # Everything else (UI pages, etc.) is unlimited
        return None

    def _check_rate_limit(self, ip: str, group: str) -> tuple[bool, int]:
        """Check if a request is within the rate limit.

        Returns:
            Tuple of (allowed, retry_after_seconds). If allowed is True,
            retry_after is 0.
        """
        now = time.monotonic()

        # Get group config
        if group == "login":
            group_config = self.config.login
        else:
            group_config = self.config.api

        window_seconds = group_config.window_seconds
        max_requests = group_config.requests

        # Get or create the window deque for this group+IP
        if group not in self._windows:
            self._windows[group] = {}
        ip_windows = self._windows[group]

        if ip not in ip_windows:
            ip_windows[ip] = deque()
        timestamps = ip_windows[ip]

        # Prune expired timestamps
        cutoff = now - window_seconds
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        # Check limit
        if len(timestamps) >= max_requests:
            # Calculate retry-after from the oldest timestamp in the window
            oldest = timestamps[0]
            retry_after = int(oldest + window_seconds - now) + 1
            return (False, max(retry_after, 1))

        # Allow and record
        timestamps.append(now)
        return (True, 0)

    def _make_429_response(self, retry_after: int) -> Any:
        """Create a 429 Too Many Requests ASGI response."""
        from starlette.responses import Response

        body = json.dumps(
            {
                "error_code": "DANGO-W002",
                "message": "Too many requests. Please try again later.",
            }
        )
        return Response(
            content=body,
            status_code=429,
            media_type="application/json",
            headers={"Retry-After": str(retry_after)},
        )

    def _maybe_cleanup(self) -> None:
        """Periodically prune expired timestamps and remove empty IP entries."""
        now = time.monotonic()
        if now - self._last_cleanup < _CLEANUP_INTERVAL:
            return

        self._last_cleanup = now
        for group in list(self._windows):
            ip_windows = self._windows[group]
            group_config = self.config.login if group == "login" else self.config.api
            cutoff = now - group_config.window_seconds
            stale_ips: list[str] = []
            for ip, dq in ip_windows.items():
                while dq and dq[0] <= cutoff:
                    dq.popleft()
                if not dq:
                    stale_ips.append(ip)
            for ip in stale_ips:
                del ip_windows[ip]
