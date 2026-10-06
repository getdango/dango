"""tests/unit/test_metabase_proxy_encoding.py

Regression tests for C13: the /metabase proxy must not forward the browser's Accept-Encoding.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.web.routes.metabase_proxy import router as proxy_router

_GET_MB_URL = "dango.web.routes.metabase_proxy._get_metabase_url"
_JS = b"console.log('metabase');"


def _make_app(tmp_path: Path, handler: Any) -> tuple[FastAPI, httpx.AsyncClient]:
    app = FastAPI()
    app.state.project_root = tmp_path
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.http_client = http_client

    user = MagicMock()
    user.email = "proxy@example.com"

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    app.include_router(proxy_router)
    return app, http_client


def _get(
    tmp_path: Path, handler: Any, headers: dict[str, str] | None = None
) -> tuple[httpx.Response, httpx.AsyncClient]:
    app, http_client = _make_app(tmp_path, handler)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("metabase.SESSION", "sess")
    with patch(_GET_MB_URL, return_value="http://mb:3000"):
        resp = client.get("/metabase/app/dist/app-main.js", headers=headers or {})
    return resp, http_client


@pytest.mark.unit
@pytest.mark.parametrize("browser_value", ["gzip, deflate, br, zstd", "br", "zstd", "identity"])
def test_proxy_sends_httpx_default_accept_encoding(tmp_path: Path, browser_value: str) -> None:
    """The browser's Accept-Encoding is never what reaches Metabase."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_JS)

    resp, http_client = _get(tmp_path, handler, {"Accept-Encoding": browser_value})

    assert resp.status_code == 200
    assert len(seen) == 1
    assert seen[0].headers["accept-encoding"] == http_client.headers["accept-encoding"]
    assert seen[0].headers["accept-encoding"] != browser_value


@pytest.mark.unit
def test_proxy_without_browser_accept_encoding_uses_client_default(tmp_path: Path) -> None:
    """With no browser Accept-Encoding, upstream sees the client default."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_JS)

    resp, http_client = _get(tmp_path, handler)

    assert resp.status_code == 200
    assert seen[0].headers["accept-encoding"] == http_client.headers["accept-encoding"]


@pytest.mark.unit
def test_proxy_decodes_gzip_and_strips_content_encoding(tmp_path: Path) -> None:
    """Control: a gzip upstream body is decoded and Content-Encoding is removed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(_JS),
            headers={"Content-Encoding": "gzip", "Content-Type": "text/javascript"},
        )

    resp, _ = _get(tmp_path, handler, {"Accept-Encoding": "gzip"})

    assert resp.content == _JS
    assert "content-encoding" not in resp.headers
