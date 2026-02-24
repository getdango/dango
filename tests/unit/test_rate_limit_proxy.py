"""tests/unit/test_rate_limit_proxy.py

Unit tests for X-Forwarded-For extraction in the rate limit middleware
behind a trusted reverse proxy (dango/web/middleware/rate_limit.py).
"""

from __future__ import annotations

import pytest

from dango.config.models import RateLimitConfig


def _make_scope(
    *,
    client_ip: str = "127.0.0.1",
    xff: str | None = None,
    path: str = "/api/sources",
    method: str = "GET",
) -> dict:
    """Build a minimal ASGI HTTP scope for testing."""
    headers: list[tuple[bytes, bytes]] = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("latin-1")))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "client": (client_ip, 12345),
        "headers": headers,
    }


@pytest.mark.unit
class TestGetClientIpProxy:
    """Tests for _get_client_ip() with trusted proxy support."""

    def test_no_trusted_proxies_returns_peer(self):
        """Without trusted proxies, returns the direct peer IP."""
        from dango.web.middleware.rate_limit import RateLimitMiddleware

        config = RateLimitConfig(trusted_proxies=[])
        mw = RateLimitMiddleware(app=None, config=config)
        scope = _make_scope(client_ip="203.0.113.42", xff="10.0.0.1")
        assert mw._get_client_ip(scope) == "203.0.113.42"

    def test_untrusted_peer_returns_peer(self):
        """If peer is NOT in trusted_proxies, returns peer regardless of XFF."""
        from dango.web.middleware.rate_limit import RateLimitMiddleware

        config = RateLimitConfig(trusted_proxies=["127.0.0.1"])
        mw = RateLimitMiddleware(app=None, config=config)
        scope = _make_scope(client_ip="203.0.113.42", xff="10.0.0.1")
        assert mw._get_client_ip(scope) == "203.0.113.42"

    def test_trusted_peer_extracts_xff(self):
        """When peer is trusted, extracts real IP from X-Forwarded-For."""
        from dango.web.middleware.rate_limit import RateLimitMiddleware

        config = RateLimitConfig(trusted_proxies=["127.0.0.1"])
        mw = RateLimitMiddleware(app=None, config=config)
        scope = _make_scope(client_ip="127.0.0.1", xff="203.0.113.42")
        assert mw._get_client_ip(scope) == "203.0.113.42"

    def test_multi_hop_xff_rightmost_non_trusted(self):
        """Multi-hop XFF: use rightmost non-trusted IP."""
        from dango.web.middleware.rate_limit import RateLimitMiddleware

        config = RateLimitConfig(trusted_proxies=["127.0.0.1", "10.0.0.1"])
        mw = RateLimitMiddleware(app=None, config=config)
        # XFF: client, proxy1, proxy2 (rightmost)
        scope = _make_scope(
            client_ip="127.0.0.1",
            xff="spoofed.ip, 203.0.113.42, 10.0.0.1",
        )
        assert mw._get_client_ip(scope) == "203.0.113.42"

    def test_all_trusted_in_xff_returns_leftmost(self):
        """When all IPs in XFF are trusted, return the leftmost."""
        from dango.web.middleware.rate_limit import RateLimitMiddleware

        config = RateLimitConfig(trusted_proxies=["127.0.0.1", "10.0.0.1"])
        mw = RateLimitMiddleware(app=None, config=config)
        scope = _make_scope(client_ip="127.0.0.1", xff="10.0.0.1, 127.0.0.1")
        assert mw._get_client_ip(scope) == "10.0.0.1"

    def test_no_xff_header_returns_peer(self):
        """When peer is trusted but no XFF header, returns peer."""
        from dango.web.middleware.rate_limit import RateLimitMiddleware

        config = RateLimitConfig(trusted_proxies=["127.0.0.1"])
        mw = RateLimitMiddleware(app=None, config=config)
        scope = _make_scope(client_ip="127.0.0.1", xff=None)
        assert mw._get_client_ip(scope) == "127.0.0.1"

    def test_no_client_returns_empty(self):
        """No client tuple in scope → empty string."""
        from dango.web.middleware.rate_limit import RateLimitMiddleware

        config = RateLimitConfig(trusted_proxies=["127.0.0.1"])
        mw = RateLimitMiddleware(app=None, config=config)
        scope = {"type": "http", "method": "GET", "path": "/api/test", "headers": []}
        assert mw._get_client_ip(scope) == ""

    def test_xff_with_spaces(self):
        """XFF values with extra whitespace are handled correctly."""
        from dango.web.middleware.rate_limit import RateLimitMiddleware

        config = RateLimitConfig(trusted_proxies=["127.0.0.1"])
        mw = RateLimitMiddleware(app=None, config=config)
        scope = _make_scope(client_ip="127.0.0.1", xff=" 203.0.113.42 , 127.0.0.1 ")
        assert mw._get_client_ip(scope) == "203.0.113.42"


@pytest.mark.unit
class TestRateLimitWithProxy:
    """Integration-style test: proxied requests get rate-limited by real IP."""

    @pytest.mark.anyio
    async def test_proxied_requests_rate_limited_by_real_ip(self):
        """Requests via trusted proxy are rate-limited by the real client IP."""
        from dango.web.middleware.rate_limit import RateLimitMiddleware

        call_count = 0

        async def _app(scope, receive, send):
            nonlocal call_count
            call_count += 1

        config = RateLimitConfig(
            trusted_proxies=["127.0.0.1"],
            api={"requests": 2, "window_seconds": 60},
        )
        mw = RateLimitMiddleware(app=_app, config=config)

        scope = _make_scope(client_ip="127.0.0.1", xff="203.0.113.42")

        # First 2 requests should pass
        for _ in range(2):
            sent_status = None

            async def _send(msg):
                nonlocal sent_status
                if msg.get("type") == "http.response.start":
                    sent_status = msg.get("status")

            await mw(scope, None, _send)

        # 3rd request should be rate-limited (429)
        sent_status = None

        async def _send_429(msg):
            nonlocal sent_status
            if msg.get("type") == "http.response.start":
                sent_status = msg.get("status")

        await mw(scope, None, _send_429)
        assert sent_status == 429
