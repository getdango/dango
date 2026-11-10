"""tests/unit/test_g4f3_proxy_credentials.py

The notebook, Metabase and dbt-docs proxies never hand Dango credentials to the upstream.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from dango.platform.cloud._server_templates import build_caddyfile, marimo_ws_block
from dango.web.routes.metabase_proxy import router as metabase_router

_GET_MB_URL = "dango.web.routes.metabase_proxy._get_metabase_url"
_REAL_CLIENT = (
    httpx.AsyncClient
)  # patching ``module.httpx.AsyncClient`` replaces the class globally
_SECRET_HEADERS = {
    "Cookie": "dango_session=DANGO-COOKIE; metabase.SESSION=mb-sess; other=x",
    "Authorization": "Bearer dango_ak_secretkey",
    "X-CSRF-Protection": "1",
    "X-Requested-With": "XMLHttpRequest",
}


def _recording_client(seen: list[httpx.Request]) -> Callable[..., httpx.AsyncClient]:
    """A factory standing in for ``httpx.AsyncClient`` whose upstream records every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(handler))

    return factory


def _starlette_request(headers: dict[str, str], method: str = "GET") -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": "/notebooks/marimo/",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


@pytest.mark.unit
def test_marimo_proxy_forwards_no_dango_credentials() -> None:
    """The fake Marimo upstream sees an allow-list of headers, never cookies or tokens."""
    from dango.notebooks.proxy import proxy_to_marimo

    seen: list[httpx.Request] = []
    headers = {
        **_SECRET_HEADERS,
        "Accept": "text/html",
        "Content-Type": "application/json",
        "Origin": "https://dango.example",
        "Marimo-Session-Id": "s_123",
        "Marimo-Server-Token": "tok",
    }
    with patch("dango.notebooks.proxy.httpx.AsyncClient", _recording_client(seen)):
        resp = asyncio.run(proxy_to_marimo(_starlette_request(headers), "/notebooks/marimo/", 7805))

    assert resp.status_code == 200
    assert len(seen) == 1
    got = {k.lower() for k in seen[0].headers}
    for name in ("cookie", "authorization", "x-csrf-protection", "x-requested-with"):
        assert name not in got, name
    assert "DANGO-COOKIE" not in str(seen[0].headers)
    assert "dango_ak_secretkey" not in str(seen[0].headers)
    # Marimo's own headers and the content negotiation headers still reach it
    assert seen[0].headers["marimo-session-id"] == "s_123"
    assert seen[0].headers["marimo-server-token"] == "tok"
    assert seen[0].headers["accept"] == "text/html"
    assert seen[0].headers["content-type"] == "application/json"
    assert seen[0].headers["origin"] == "https://dango.example"


@pytest.mark.unit
@pytest.mark.parametrize("domain", [None, "dango.example"])
def test_caddy_marimo_ws_reverse_proxy_strips_cookie_and_authorization(domain: str | None) -> None:
    """The WebSocket handle strips credentials on the upstream leg, not on forward_auth."""
    text = build_caddyfile(domain=domain, marimo_port=7805)
    block = marimo_ws_block(7805)
    assert block in text

    forward = block[block.index("forward_auth") : block.index("reverse_proxy")]
    proxy = block[block.index("reverse_proxy") :]
    # forward_auth needs the cookie to authenticate the user: it must not be stripped there
    assert "header_up -Cookie" not in forward
    assert "header_up -Authorization" not in forward
    assert "header_up -Cookie" in proxy
    assert "header_up -Authorization" in proxy
    assert proxy.startswith("reverse_proxy localhost:7805 {")
    assert proxy.count("{") == proxy.count("}")


def _metabase_app(tmp_path: Path, seen: list[httpx.Request]) -> FastAPI:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    app = FastAPI()
    app.state.project_root = tmp_path
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    user = MagicMock()
    user.email = "proxy@example.com"

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    app.include_router(metabase_router)
    return app


@pytest.mark.unit
def test_metabase_proxy_keeps_only_the_metabase_session_cookie(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    client = TestClient(_metabase_app(tmp_path, seen), raise_server_exceptions=False)
    client.cookies.set("dango_session", "DANGO-COOKIE")
    client.cookies.set("metabase.SESSION", "mb-sess")
    client.cookies.set("other", "x")
    with patch(_GET_MB_URL, return_value="http://mb:3000"):
        resp = client.get(
            "/metabase/api/user/current",
            headers={"Authorization": "Bearer dango_ak_secretkey"},
        )

    assert resp.status_code == 200
    assert len(seen) == 1
    assert seen[0].headers["cookie"] == "metabase.SESSION=mb-sess"
    assert "authorization" not in {k.lower() for k in seen[0].headers}
    assert "DANGO-COOKIE" not in str(seen[0].headers)
    assert "dango_ak_secretkey" not in str(seen[0].headers)


@pytest.mark.unit
def test_metabase_proxy_sends_no_cookie_without_a_metabase_session(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    client = TestClient(_metabase_app(tmp_path, seen), raise_server_exceptions=False)
    with patch(_GET_MB_URL, return_value="http://mb:3000"):
        client.get(
            "/app/dist/x.js",
            headers={"Cookie": "dango_session=DANGO-COOKIE", "Authorization": "Bearer abc"},
        )
    assert len(seen) == 1
    assert "cookie" not in {k.lower() for k in seen[0].headers}
    assert "authorization" not in {k.lower() for k in seen[0].headers}


@pytest.mark.unit
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_api_health_is_get_only(tmp_path: Path, method: str) -> None:
    """The unauthenticated health route reaches Metabase for GET only."""
    seen: list[httpx.Request] = []
    client = TestClient(_metabase_app(tmp_path, seen), raise_server_exceptions=False)
    with patch(_GET_MB_URL, return_value="http://mb:3000"):
        resp = client.request(method, "/api/health", content=b"{}")
        assert resp.status_code == 405
        assert client.get("/api/health").status_code == 200
    assert [r.method for r in seen] == ["GET"]


@pytest.mark.unit
def test_dbt_docs_proxy_forwards_no_credentials() -> None:
    from dango.web.routes.dbt import router as dbt_router

    seen: list[httpx.Request] = []
    app = FastAPI()
    app.include_router(dbt_router)
    client = TestClient(app, raise_server_exceptions=False)
    with patch("dango.web.routes.dbt.httpx.AsyncClient", _recording_client(seen)):
        resp = client.get(
            "/dbt-docs/index.html",
            headers={"Cookie": "dango_session=DANGO-COOKIE", "Authorization": "Bearer dango_ak_x"},
        )
    assert resp.status_code == 200
    assert len(seen) == 1
    names = {k.lower() for k in seen[0].headers}
    assert "cookie" not in names
    assert "authorization" not in names
