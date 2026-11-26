"""dango/web/proxy_client.py

Factory for the long-lived HTTP clients that talk to upstream services on behalf of many users.

httpx clients keep a cookie jar and attach its cookies to any later request that has no ``Cookie``
header of its own. A client shared by every user must never do that, so the clients built here
refuse to store or return cookies.
"""

from __future__ import annotations

from http.cookiejar import Cookie, CookieJar, CookiePolicy
from urllib.request import Request

import httpx

PROXY_TIMEOUT_SECONDS = 30.0


class NoCookiesPolicy(CookiePolicy):
    """A cookie policy that never stores a cookie and never returns one."""

    netscape = False
    rfc2965 = False
    hide_cookie2 = True

    def set_ok(self, cookie: Cookie, request: Request) -> bool:
        """Never store a cookie."""
        return False

    def return_ok(self, cookie: Cookie, request: Request) -> bool:
        """Never send a cookie."""
        return False

    def domain_return_ok(self, domain: str, request: Request) -> bool:
        """No domain has cookies to send."""
        return False

    def path_return_ok(self, path: str, request: Request) -> bool:
        """No path has cookies to send."""
        return False


class _EmptyCookieJar(CookieJar):
    """A jar that stays empty even for a cookie added directly (``client.cookies.set``).

    httpx copies the client's jar into a fresh one for every request, and the copy has the
    default policy, so a policy alone cannot stop a cookie that was put into the jar by hand.
    """

    def set_cookie(self, cookie: Cookie) -> None:
        """Drop the cookie."""
        return None


def forbid_cookies(client: httpx.AsyncClient | httpx.Client) -> None:
    """Make an existing client unable to store, keep or send cookies."""
    client.cookies.jar = _EmptyCookieJar(policy=NoCookiesPolicy())


def make_proxy_client(*, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The shared async client used to proxy requests (no redirects, no cookie memory).

    ``transport`` is for tests: a double built here differs from production only in the wire.
    """
    client = httpx.AsyncClient(
        follow_redirects=False, timeout=PROXY_TIMEOUT_SECONDS, transport=transport
    )
    forbid_cookies(client)
    return client
