"""tests/unit/test_metabase_cookies.py

``upstream_session_cookie`` reads the value and the expiry of Metabase's ``metabase.SESSION``
Set-Cookie, and never raises on a malformed header.
"""

from __future__ import annotations

import httpx
import pytest

from dango.web.routes.metabase_cookies import upstream_session_cookie

_FUTURE = "Fri, 01 Jan 2100 00:00:00 GMT"


def _read(*set_cookies: str) -> tuple[str, bool] | None:
    response = httpx.Response(200, headers=[("set-cookie", value) for value in set_cookies])
    return upstream_session_cookie(response)


@pytest.mark.unit
@pytest.mark.parametrize(
    "header",
    [
        "metabase.SESSION=; Path=/",
        "metabase.SESSION=abc; Path=/; Max-Age=0",
        "metabase.SESSION=abc; Path=/; Max-Age=-5",
        "metabase.SESSION=abc; Path=/; Expires=Thu, 01-Jan-1970 00:00:00 GMT",
        "metabase.SESSION=abc; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
        "metabase.SESSION=abc; Path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; max-age=3600",
    ],
    ids=[
        "empty-value",
        "max-age-zero",
        "max-age-negative",
        "expires-jetty-past",
        "expires-rfc1123-past",
        "past-expires-beats-positive-max-age",
    ],
)
def test_expiring_cookies_are_reported_as_expiry(header: str) -> None:
    found = _read(header)
    assert found is not None
    assert found[1] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("metabase.SESSION=abc; Path=/; HttpOnly; SameSite=Lax", "abc"),
        ("metabase.SESSION=abc; Path=/; Max-Age=3600", "abc"),
        (f"metabase.SESSION=abc; Path=/; Expires={_FUTURE}", "abc"),
        (f"metabase.SESSION=abc; Path=/; Max-Age=3600; Expires={_FUTURE}", "abc"),
    ],
    ids=[
        "plain-session-cookie",
        "positive-max-age",
        "future-expires",
        "max-age-and-future-expires",
    ],
)
def test_live_cookies_are_reported_with_their_value(header: str, value: str) -> None:
    assert _read(header) == (value, False)


@pytest.mark.unit
@pytest.mark.parametrize(
    "header",
    [
        "metabase.SESSION=abc; Max-Age=soon",
        "metabase.SESSION=abc; Max-Age=",
        "metabase.SESSION=abc; Expires=not a date",
        "metabase.SESSION=abc; Expires=",
        "metabase.SESSION=abc; Max-Age; Expires",
    ],
    ids=[
        "max-age-text",
        "max-age-empty",
        "expires-text",
        "expires-empty",
        "attributes-without-value",
    ],
)
def test_unreadable_attributes_are_ignored_and_never_raise(header: str) -> None:
    assert _read(header) == ("abc", False)


@pytest.mark.unit
@pytest.mark.parametrize(
    "headers",
    [
        (),
        ("",),
        ("no-equals-sign",),
        ("metabase.SESSIONX=abc",),
        ("metabase.DEVICE=d; Path=/", "metabase.TIMEOUT=alive; Max-Age=1209600"),
    ],
    ids=["no-header", "empty-header", "no-equals", "other-name", "other-cookies-only"],
)
def test_responses_without_a_session_cookie_give_none(headers: tuple[str, ...]) -> None:
    assert _read(*headers) is None


@pytest.mark.unit
def test_only_the_session_cookie_is_read_from_several_set_cookie_headers() -> None:
    found = _read(
        "metabase.DEVICE=device; Path=/; Max-Age=0",
        "metabase.SESSION=abc; Path=/; HttpOnly",
        "metabase.TIMEOUT=; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
    )
    assert found == ("abc", False)


@pytest.mark.unit
def test_the_last_session_cookie_wins() -> None:
    assert _read("metabase.SESSION=first", "metabase.SESSION=; Max-Age=0") == ("", True)
