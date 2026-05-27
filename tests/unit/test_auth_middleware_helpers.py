"""tests/unit/test_auth_middleware_helpers.py

Tests for header parsing helpers and the is_secure_request utility
in dango/web/middleware/auth.py.

Split from test_auth_middleware.py to stay under the 500-line limit.
"""

from __future__ import annotations

from typing import Any

import pytest

from dango.web.middleware.auth import (
    COOKIE_NAME,
    _has_csrf_header,
    _is_browser_request,
    _parse_bearer_token,
    _parse_cookie,
    is_secure_request,
)

# ---------------------------------------------------------------------------
# is_secure_request tests
# ---------------------------------------------------------------------------


def _make_scope(
    scheme: str = "http",
    server: tuple[str, int] = ("127.0.0.1", 8080),
) -> dict[str, Any]:
    """Create a minimal scope for is_secure_request testing."""
    return {"type": "http", "scheme": scheme, "server": server}


@pytest.mark.unit
class TestSecureRequestDetection:
    """Tests for is_secure_request() utility."""

    def test_localhost_not_secure(self) -> None:
        """Localhost connections are not secure."""
        assert is_secure_request(_make_scope(server=("127.0.0.1", 8080))) is False

    def test_localhost_name_not_secure(self) -> None:
        """'localhost' hostname is not secure."""
        assert is_secure_request(_make_scope(server=("localhost", 8080))) is False

    def test_https_is_secure(self) -> None:
        """HTTPS scheme is always secure."""
        assert is_secure_request(_make_scope(scheme="https")) is True

    def test_non_localhost_http_is_secure(self) -> None:
        """Non-localhost HTTP implies production behind a proxy."""
        assert is_secure_request(_make_scope(server=("10.0.0.1", 8080))) is True

    def test_ipv6_localhost_not_secure(self) -> None:
        """IPv6 localhost is not secure."""
        assert is_secure_request(_make_scope(server=("::1", 8080))) is False

    def test_no_server_not_secure(self) -> None:
        """Missing server key: not secure."""
        assert is_secure_request({"type": "http", "scheme": "http"}) is False


# ---------------------------------------------------------------------------
# Header parsing helper tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCookieParsing:
    """Tests for _parse_cookie()."""

    def test_present(self) -> None:
        """Extracts named cookie from Cookie header."""
        headers: list[tuple[bytes, bytes]] = [
            (b"cookie", b"other=abc; dango_session=mytoken123; foo=bar")
        ]
        assert _parse_cookie(headers, COOKIE_NAME) == "mytoken123"

    def test_missing(self) -> None:
        """Returns None when cookie name is not in header."""
        headers: list[tuple[bytes, bytes]] = [(b"cookie", b"other=abc")]
        assert _parse_cookie(headers, COOKIE_NAME) is None

    def test_no_header(self) -> None:
        """Returns None when no Cookie header exists."""
        assert _parse_cookie([], COOKIE_NAME) is None

    def test_only_cookie(self) -> None:
        """Extracts cookie when it's the only one."""
        headers: list[tuple[bytes, bytes]] = [(b"cookie", b"dango_session=tok")]
        assert _parse_cookie(headers, COOKIE_NAME) == "tok"


@pytest.mark.unit
class TestBearerParsing:
    """Tests for _parse_bearer_token()."""

    def test_valid_bearer(self) -> None:
        """Extracts Bearer token from Authorization header."""
        headers: list[tuple[bytes, bytes]] = [(b"authorization", b"Bearer dango_ak_abc123")]
        assert _parse_bearer_token(headers) == "dango_ak_abc123"

    def test_not_bearer(self) -> None:
        """Returns None for non-Bearer Authorization."""
        headers: list[tuple[bytes, bytes]] = [(b"authorization", b"Basic abc123")]
        assert _parse_bearer_token(headers) is None

    def test_empty_token(self) -> None:
        """Returns None for 'Bearer ' with empty token."""
        headers: list[tuple[bytes, bytes]] = [(b"authorization", b"Bearer ")]
        assert _parse_bearer_token(headers) is None

    def test_no_auth_header(self) -> None:
        """Returns None when no Authorization header exists."""
        assert _parse_bearer_token([]) is None


@pytest.mark.unit
class TestBrowserRequestDetection:
    """Tests for _is_browser_request()."""

    def test_html_accept(self) -> None:
        """Returns True when Accept contains text/html."""
        headers: list[tuple[bytes, bytes]] = [(b"accept", b"text/html,application/xhtml+xml")]
        assert _is_browser_request(headers) is True

    def test_json_accept(self) -> None:
        """Returns False when Accept is JSON only."""
        headers: list[tuple[bytes, bytes]] = [(b"accept", b"application/json")]
        assert _is_browser_request(headers) is False

    def test_no_accept(self) -> None:
        """Returns False when no Accept header exists."""
        assert _is_browser_request([]) is False


@pytest.mark.unit
class TestCsrfHeaderDetection:
    """Tests for _has_csrf_header()."""

    def test_x_requested_with(self) -> None:
        """Returns True for X-Requested-With header."""
        headers: list[tuple[bytes, bytes]] = [(b"x-requested-with", b"XMLHttpRequest")]
        assert _has_csrf_header(headers) is True

    def test_x_csrf_protection(self) -> None:
        """Returns True for X-CSRF-Protection header."""
        headers: list[tuple[bytes, bytes]] = [(b"x-csrf-protection", b"1")]
        assert _has_csrf_header(headers) is True

    def test_missing(self) -> None:
        """Returns False when no CSRF header is present."""
        assert _has_csrf_header([]) is False
