"""dango/web/middleware/cors.py

CORS with credentials for an explicit origin allowlist: the cloud origin, or Dango's own loopback origins.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_LOOPBACK_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]")
_ALLOWED_METHODS = ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT")
_PREFLIGHT_MAX_AGE = "600"


def own_origins(host_header: str | None) -> frozenset[str]:
    """Dango's own browser origins for a request addressed to *host_header*.

    The app is created before the server binds a port, so the port a browser really
    used is read from the request's ``Host`` header: ``localhost``, ``127.0.0.1`` and
    ``[::1]`` on that port, over ``http`` (local mode has no TLS). A missing or
    malformed ``Host`` yields no origins.
    """
    if not host_header:
        return frozenset()
    try:
        port = urlsplit("//" + host_header.strip()).port or 80
    except ValueError:
        return frozenset()
    suffix = "" if port == 80 else f":{port}"  # browsers leave the default port out of Origin
    return frozenset(f"http://{host}{suffix}" for host in _LOOPBACK_HOSTS)


class CredentialedCORSMiddleware:
    """CORS that never combines credentials with an origin it was not told to trust.

    *allow_origins* is the fixed list of trusted origins (the cloud origin; empty means
    cross-origin access is refused). ``None`` selects local mode: the trusted origins are
    Dango's own, derived per request by :func:`own_origins`. A trusted origin is reflected
    with ``Access-Control-Allow-Credentials``; any other origin gets neither header (a browser
    then withholds the response from the page). Requests without an ``Origin`` header
    (CLI, MCP, curl) pass through unchanged.
    """

    def __init__(self, app: ASGIApp, allow_origins: Sequence[str] | None = None) -> None:
        self.app = app
        self._fixed_origins: frozenset[str] | None = (
            None if allow_origins is None else frozenset(allow_origins)
        )

    def _is_allowed(self, origin: str, headers: Headers) -> bool:
        if self._fixed_origins is not None:
            return origin in self._fixed_origins
        return origin in own_origins(headers.get("host"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is None:
            await self.app(scope, receive, send)
            return

        allowed = self._is_allowed(origin, headers)
        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            response = self._preflight(headers, origin, allowed)
            await response(scope, receive, send)
            return

        async def send_with_cors(message: Message) -> None:
            """Add the CORS response headers to the start of the response."""
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                response_headers = MutableHeaders(scope=message)
                response_headers.add_vary_header("Origin")
                if allowed:
                    response_headers["Access-Control-Allow-Origin"] = origin
                    response_headers["Access-Control-Allow-Credentials"] = "true"
            await send(message)

        await self.app(scope, receive, send_with_cors)

    @staticmethod
    def _preflight(headers: Headers, origin: str, allowed: bool) -> Response:
        if not allowed:
            return PlainTextResponse(
                "Disallowed CORS origin", status_code=400, headers={"Vary": "Origin"}
            )
        if headers["access-control-request-method"] not in _ALLOWED_METHODS:
            return PlainTextResponse(
                "Disallowed CORS method", status_code=400, headers={"Vary": "Origin"}
            )
        response_headers = {
            "Vary": "Origin",
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": ", ".join(_ALLOWED_METHODS),
            "Access-Control-Max-Age": _PREFLIGHT_MAX_AGE,
        }
        requested_headers = headers.get("access-control-request-headers")
        if requested_headers is not None:
            response_headers["Access-Control-Allow-Headers"] = requested_headers
        return PlainTextResponse("OK", status_code=200, headers=response_headers)
