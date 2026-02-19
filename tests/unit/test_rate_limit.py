"""tests/unit/test_rate_limit.py

Tests for the rate limiting ASGI middleware in dango/web/middleware/rate_limit.py.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any
from unittest.mock import patch

import pytest

from dango.config.models import RateLimitConfig, RateLimitGroupConfig
from dango.web.middleware.rate_limit import RateLimitMiddleware

# ---------------------------------------------------------------------------
# ASGI test helpers
# ---------------------------------------------------------------------------


async def _noop_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Minimal ASGI app that returns a 200 OK response."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"OK"})


def _make_scope(
    path: str = "/api/sources",
    method: str = "GET",
    client_ip: str = "1.2.3.4",
    scope_type: str = "http",
) -> dict[str, Any]:
    """Create a minimal ASGI HTTP scope dict."""
    scope: dict[str, Any] = {
        "type": scope_type,
        "method": method,
        "path": path,
        "client": (client_ip, 12345),
        "headers": [],
    }
    return scope


async def _collect_response(
    middleware: RateLimitMiddleware,
    scope: dict[str, Any],
) -> dict[str, Any]:
    """Run middleware and collect the response status, headers, and body."""
    result: dict[str, Any] = {"status": 0, "headers": {}, "body": b""}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
            for name, value in message.get("headers", []):
                if isinstance(name, bytes):
                    result["headers"][name.decode()] = value.decode()
                else:
                    result["headers"][name] = value
        elif message["type"] == "http.response.body":
            result["body"] += message.get("body", b"")

    await middleware(scope, receive, send)
    return result


# ---------------------------------------------------------------------------
# Route classification tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRouteClassification:
    """Tests for _classify_route()."""

    def test_login_path(self) -> None:
        """Login endpoint is classified as 'login' group."""
        mw = RateLimitMiddleware(_noop_app)
        assert mw._classify_route("/api/auth/login") == "login"

    def test_health_paths_unlimited(self) -> None:
        """Health and status paths are unlimited (None)."""
        mw = RateLimitMiddleware(_noop_app)
        assert mw._classify_route("/api/status") is None
        assert mw._classify_route("/api/watcher/status") is None
        assert mw._classify_route("/api/health/platform") is None

    def test_api_paths(self) -> None:
        """General API paths are classified as 'api' group."""
        mw = RateLimitMiddleware(_noop_app)
        assert mw._classify_route("/api/sources") == "api"
        assert mw._classify_route("/api/config") == "api"
        assert mw._classify_route("/api/dbt/models") == "api"

    def test_static_paths_unlimited(self) -> None:
        """Static asset paths are unlimited."""
        mw = RateLimitMiddleware(_noop_app)
        assert mw._classify_route("/static/css/main.css") is None
        assert mw._classify_route("/static/js/app.js") is None

    def test_ui_pages_unlimited(self) -> None:
        """UI pages are unlimited."""
        mw = RateLimitMiddleware(_noop_app)
        assert mw._classify_route("/") is None
        assert mw._classify_route("/health") is None
        assert mw._classify_route("/logs") is None

    def test_websocket_unlimited(self) -> None:
        """WebSocket path is unlimited."""
        mw = RateLimitMiddleware(_noop_app)
        assert mw._classify_route("/ws") is None

    def test_metabase_proxy(self) -> None:
        """Metabase proxy paths are classified as 'api' group."""
        mw = RateLimitMiddleware(_noop_app)
        assert mw._classify_route("/metabase/api/session") == "api"

    def test_dbt_docs_unlimited(self) -> None:
        """dbt docs paths are unlimited."""
        mw = RateLimitMiddleware(_noop_app)
        assert mw._classify_route("/dbt-docs/index.html") is None

    def test_api_docs_unlimited(self) -> None:
        """API documentation paths are unlimited."""
        mw = RateLimitMiddleware(_noop_app)
        assert mw._classify_route("/api/docs") is None
        assert mw._classify_route("/api/redoc") is None
        assert mw._classify_route("/api") is None


# ---------------------------------------------------------------------------
# Client IP extraction tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestClientIPExtraction:
    """Tests for _get_client_ip()."""

    def test_from_scope_client(self) -> None:
        """Extracts IP from scope client tuple."""
        mw = RateLimitMiddleware(_noop_app)
        scope = _make_scope(client_ip="10.0.0.1")
        assert mw._get_client_ip(scope) == "10.0.0.1"

    def test_missing_client(self) -> None:
        """Returns empty string when client is missing."""
        mw = RateLimitMiddleware(_noop_app)
        scope: dict[str, Any] = {"type": "http", "method": "GET", "path": "/api/sources"}
        assert mw._get_client_ip(scope) == ""


# ---------------------------------------------------------------------------
# Sliding window tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSlidingWindow:
    """Tests for _check_rate_limit() sliding window logic."""

    def test_allows_within_limit(self) -> None:
        """Requests within the limit are allowed."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=5, window_seconds=60))
        mw = RateLimitMiddleware(_noop_app, config=config)

        for _ in range(5):
            allowed, retry_after = mw._check_rate_limit("1.2.3.4", "api")
            assert allowed is True
            assert retry_after == 0

    def test_blocks_when_exceeded(self) -> None:
        """Requests exceeding the limit are blocked."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=3, window_seconds=60))
        mw = RateLimitMiddleware(_noop_app, config=config)

        for _ in range(3):
            mw._check_rate_limit("1.2.3.4", "api")

        allowed, retry_after = mw._check_rate_limit("1.2.3.4", "api")
        assert allowed is False
        assert retry_after > 0

    def test_window_expires(self) -> None:
        """Timestamps outside the window are pruned, allowing new requests."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=2, window_seconds=60))
        mw = RateLimitMiddleware(_noop_app, config=config)

        base_time = 1000.0

        with patch("dango.web.middleware.rate_limit.time") as mock_time:
            mock_time.monotonic.return_value = base_time
            mw._check_rate_limit("1.2.3.4", "api")
            mw._check_rate_limit("1.2.3.4", "api")

            # Now jump past the window
            mock_time.monotonic.return_value = base_time + 61.0
            allowed, retry_after = mw._check_rate_limit("1.2.3.4", "api")
            assert allowed is True

    def test_independent_per_ip(self) -> None:
        """Different IPs have independent rate limits."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=2, window_seconds=60))
        mw = RateLimitMiddleware(_noop_app, config=config)

        mw._check_rate_limit("1.1.1.1", "api")
        mw._check_rate_limit("1.1.1.1", "api")

        allowed, _ = mw._check_rate_limit("2.2.2.2", "api")
        assert allowed is True

    def test_independent_per_group(self) -> None:
        """Different groups have independent rate limits."""
        config = RateLimitConfig(
            login=RateLimitGroupConfig(requests=2, window_seconds=60),
            api=RateLimitGroupConfig(requests=2, window_seconds=60),
        )
        mw = RateLimitMiddleware(_noop_app, config=config)

        mw._check_rate_limit("1.2.3.4", "login")
        mw._check_rate_limit("1.2.3.4", "login")

        allowed, _ = mw._check_rate_limit("1.2.3.4", "api")
        assert allowed is True


# ---------------------------------------------------------------------------
# Full middleware integration tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRateLimitMiddleware:
    """Tests for the full middleware ASGI flow."""

    def test_disabled_passthrough(self) -> None:
        """Disabled rate limiting passes all requests through."""
        config = RateLimitConfig(enabled=False)
        mw = RateLimitMiddleware(_noop_app, config=config)

        scope = _make_scope(path="/api/sources", client_ip="1.2.3.4")
        result = asyncio.run(_collect_response(mw, scope))
        assert result["status"] == 200

    def test_localhost_exempt(self) -> None:
        """Localhost IPs are always allowed."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=1, window_seconds=60))
        mw = RateLimitMiddleware(_noop_app, config=config)

        for ip in ("127.0.0.1", "::1"):
            # Make many requests from localhost — all should pass
            for _ in range(5):
                scope = _make_scope(path="/api/sources", client_ip=ip)
                result = asyncio.run(_collect_response(mw, scope))
                assert result["status"] == 200

    def test_options_exempt(self) -> None:
        """OPTIONS requests are always allowed (CORS preflight)."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=1, window_seconds=60))
        mw = RateLimitMiddleware(_noop_app, config=config)

        for _ in range(5):
            scope = _make_scope(path="/api/sources", method="OPTIONS", client_ip="1.2.3.4")
            result = asyncio.run(_collect_response(mw, scope))
            assert result["status"] == 200

    def test_non_http_passthrough(self) -> None:
        """Non-HTTP scopes (e.g., WebSocket) pass through."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=1, window_seconds=60))

        ws_received = False

        async def ws_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            nonlocal ws_received
            ws_received = True

        mw = RateLimitMiddleware(ws_app, config=config)
        scope = _make_scope(scope_type="websocket")

        async def run() -> None:
            await mw(scope, None, None)

        asyncio.run(run())
        assert ws_received is True

    def test_429_response_format(self) -> None:
        """429 response has correct JSON body and error code."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=1, window_seconds=60))
        mw = RateLimitMiddleware(_noop_app, config=config)

        scope = _make_scope(path="/api/sources", client_ip="1.2.3.4")
        asyncio.run(_collect_response(mw, scope))

        # Second request should be rate-limited
        result = asyncio.run(_collect_response(mw, scope))
        assert result["status"] == 429

        body = json.loads(result["body"])
        assert body["error_code"] == "DANGO-W002"
        assert "Too many requests" in body["message"]

    def test_retry_after_header(self) -> None:
        """429 response includes Retry-After header."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=1, window_seconds=60))
        mw = RateLimitMiddleware(_noop_app, config=config)

        scope = _make_scope(path="/api/sources", client_ip="1.2.3.4")
        asyncio.run(_collect_response(mw, scope))

        result = asyncio.run(_collect_response(mw, scope))
        assert result["status"] == 429
        assert "retry-after" in result["headers"]
        assert int(result["headers"]["retry-after"]) > 0

    def test_login_stricter_limit(self) -> None:
        """Login endpoint has its own stricter limit."""
        config = RateLimitConfig(
            login=RateLimitGroupConfig(requests=2, window_seconds=60),
            api=RateLimitGroupConfig(requests=100, window_seconds=60),
        )
        mw = RateLimitMiddleware(_noop_app, config=config)

        scope = _make_scope(path="/api/auth/login", method="POST", client_ip="1.2.3.4")
        asyncio.run(_collect_response(mw, scope))
        asyncio.run(_collect_response(mw, scope))

        # Third login attempt should be blocked
        result = asyncio.run(_collect_response(mw, scope))
        assert result["status"] == 429

        # API endpoint should still work
        api_scope = _make_scope(path="/api/sources", client_ip="1.2.3.4")
        result = asyncio.run(_collect_response(mw, api_scope))
        assert result["status"] == 200

    def test_api_limit(self) -> None:
        """General API limit is applied to API paths."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=3, window_seconds=60))
        mw = RateLimitMiddleware(_noop_app, config=config)

        scope = _make_scope(path="/api/sources", client_ip="1.2.3.4")
        for _ in range(3):
            result = asyncio.run(_collect_response(mw, scope))
            assert result["status"] == 200

        result = asyncio.run(_collect_response(mw, scope))
        assert result["status"] == 429

    def test_health_unlimited(self) -> None:
        """Health endpoints are never rate-limited."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=1, window_seconds=60))
        mw = RateLimitMiddleware(_noop_app, config=config)

        for _ in range(5):
            scope = _make_scope(path="/api/status", client_ip="1.2.3.4")
            result = asyncio.run(_collect_response(mw, scope))
            assert result["status"] == 200


# ---------------------------------------------------------------------------
# Cleanup tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMiddlewareCleanup:
    """Tests for periodic stale entry cleanup."""

    def test_stale_entries_removed(self) -> None:
        """Inactive IPs with expired timestamps are cleaned up."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=5, window_seconds=60))
        mw = RateLimitMiddleware(_noop_app, config=config)

        base_time = 1000.0

        with patch("dango.web.middleware.rate_limit.time") as mock_time:
            mock_time.monotonic.return_value = base_time
            mw._last_cleanup = base_time

            # Add entries for two IPs
            mw._check_rate_limit("1.2.3.4", "api")
            mw._check_rate_limit("5.6.7.8", "api")
            assert "1.2.3.4" in mw._windows["api"]
            assert "5.6.7.8" in mw._windows["api"]

            # Jump past window + cleanup interval
            mock_time.monotonic.return_value = base_time + 361.0

            # Cleanup should prune stale timestamps and remove both IPs
            mw._maybe_cleanup()
            assert "1.2.3.4" not in mw._windows.get("api", {})
            assert "5.6.7.8" not in mw._windows.get("api", {})

    def test_empty_deques_removed(self) -> None:
        """Deques that are already empty are removed during cleanup."""
        config = RateLimitConfig(api=RateLimitGroupConfig(requests=5, window_seconds=60))
        mw = RateLimitMiddleware(_noop_app, config=config)
        mw._windows = {"api": {"old_ip": deque()}}
        mw._last_cleanup = 0.0
        mw._maybe_cleanup()
        assert "old_ip" not in mw._windows.get("api", {})
