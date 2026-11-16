"""tests/unit/test_web_metabase_recovery.py

Tests for the browser recovery script served by and injected through the Metabase proxy (K-33).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.web.routes.metabase_proxy import router as proxy_router

_GET_MB_URL = "dango.web.routes.metabase_proxy._get_metabase_url"
_REBRIDGE = "dango.web.routes.metabase_proxy._rebridge_if_needed"
_SCRIPT_URL = "/metabase/_dango/recovery.js"
_SCRIPT_TAG = f'<script defer src="{_SCRIPT_URL}"></script>'
_HTML = b"<!doctype html><html><head><title>Metabase</title></head><body><div id='root'></div></body></html>"


def _make_client(tmp_path: Path, handler: Any) -> TestClient:
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

    app.include_router(proxy_router)
    return TestClient(app, raise_server_exceptions=False)


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("upstream down", request=request)


def _fetch(tmp_path: Path, response: httpx.Response, path: str, method: str = "GET") -> Any:
    client = _make_client(tmp_path, lambda request: response)
    client.cookies.set("metabase.SESSION", "sess")
    with patch(_GET_MB_URL, return_value="http://mb:3000"):
        return client.request(method, path)


@pytest.mark.unit
class TestMaintenanceDocument:
    def test_maintenance_document_loads_external_recovery_script_and_retry_control(
        self, tmp_path: Path
    ) -> None:
        client = _make_client(tmp_path, _down)
        with patch(_GET_MB_URL, return_value="http://mb:3000"):
            resp = client.get("/metabase/question/1")

        assert resp.status_code == 502
        assert resp.headers["cache-control"] == "no-store"
        assert f'src="{_SCRIPT_URL}"' in resp.text
        assert 'data-initial-state="down"' in resp.text
        assert "Retry now" in resp.text
        assert "Back to Dango" in resp.text
        assert "<script>" not in resp.text  # no inline script (Metabase CSP is not applied here)
        assert 'http-equiv="refresh"' not in resp.text.split("<noscript>")[0]

    def test_api_failure_stays_plain_text_without_script(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path, _down)
        with patch(_GET_MB_URL, return_value="http://mb:3000"):
            resp = client.get("/metabase/api/card")

        assert resp.status_code == 502
        assert resp.text == "Metabase is temporarily unavailable"
        assert "script" not in resp.text


@pytest.mark.unit
class TestInjection:
    def test_successful_html_response_injects_recovery_script_once(self, tmp_path: Path) -> None:
        upstream = httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})
        resp = _fetch(tmp_path, upstream, "/metabase/question/1")

        assert resp.status_code == 200
        assert resp.text.count(_SCRIPT_URL) == 1
        assert resp.text.index(_SCRIPT_TAG) < resp.text.index("</body>")
        assert resp.text.replace(_SCRIPT_TAG, "") == _HTML.decode()
        assert int(resp.headers["content-length"]) == len(resp.content)

    def test_html_already_carrying_the_script_is_not_injected_again(self, tmp_path: Path) -> None:
        body = _HTML.replace(b"</body>", _SCRIPT_TAG.encode() + b"</body>")
        upstream = httpx.Response(200, content=body, headers={"content-type": "text/html"})
        resp = _fetch(tmp_path, upstream, "/metabase/question/1")

        assert resp.text.count(_SCRIPT_URL) == 1

    @pytest.mark.parametrize(
        ("path", "method", "status", "content_type", "body"),
        [
            ("/metabase/api/card", "GET", 200, "application/json", b'{"html":"</body>"}'),
            ("/app/dist/app-main.js", "GET", 200, "application/javascript", b"var a='</body>';"),
            ("/styles.css", "GET", 200, "text/css", b"body{color:red}</body>"),
            ("/app/img/logo.png", "GET", 200, "image/png", b"\x89PNG</body>\x00"),
            ("/metabase/api/card", "GET", 500, "text/html", _HTML),
            ("/metabase/question/1", "POST", 200, "text/html", _HTML),
            ("/metabase/question/1", "GET", 200, "text/html", b"<html>no closing tag</html>"),
        ],
    )
    def test_non_html_and_asset_responses_are_byte_identical(
        self, tmp_path: Path, path: str, method: str, status: int, content_type: str, body: bytes
    ) -> None:
        upstream = httpx.Response(status, content=body, headers={"content-type": content_type})
        resp = _fetch(tmp_path, upstream, path, method)

        assert resp.status_code == status
        assert resp.content == body
        assert resp.headers["content-type"].startswith(content_type)
        assert _SCRIPT_URL not in resp.text

    def test_injection_preserves_csp_and_multiple_set_cookie_headers(self, tmp_path: Path) -> None:
        csp = "default-src 'self'; script-src 'self' 'nonce-abc'; frame-ancestors 'none'"
        upstream = httpx.Response(
            200,
            content=_HTML,
            headers=[
                ("content-type", "text/html;charset=utf-8"),
                ("content-security-policy", csp),
                ("set-cookie", "a=1; Path=/"),
                ("set-cookie", "metabase.SESSION=raw; Path=/; HttpOnly"),
                ("set-cookie", "b=2; Path=/; HttpOnly"),
            ],
        )
        resp = _fetch(tmp_path, upstream, "/metabase/")

        assert resp.headers["content-security-policy"] == csp
        assert resp.headers["content-type"] == "text/html;charset=utf-8"
        cookies = resp.headers.get_list("set-cookie")
        assert cookies == ["a=1; Path=/", "b=2; Path=/; HttpOnly"]
        assert resp.text.count(_SCRIPT_URL) == 1


@pytest.mark.unit
class TestRecoveryScript:
    def _script(self, tmp_path: Path) -> tuple[Any, list[httpx.Request]]:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"upstream")

        client = _make_client(tmp_path, handler)
        with patch(_GET_MB_URL, return_value="http://mb:3000"):
            return client.get(_SCRIPT_URL), seen

    def test_script_is_served_locally_before_the_catch_all(self, tmp_path: Path) -> None:
        resp, seen = self._script(tmp_path)

        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]
        assert resp.headers["cache-control"] in {"no-cache", "no-store"}
        assert seen == []

    def test_recovery_script_uses_no_store_consecutive_failure_gate_and_single_reload(
        self, tmp_path: Path
    ) -> None:
        js = self._script(tmp_path)[0].text

        assert "/metabase/api/health" in js
        assert 'cache: "no-store"' in js
        assert 'credentials: "same-origin"' in js
        failures = re.search(r"FAILURES_BEFORE_DOWN\s*=\s*(\d+)", js)
        assert failures is not None and int(failures.group(1)) >= 2
        visible = re.search(r"VISIBLE_INTERVAL_MS\s*=\s*(\d+)", js)
        assert visible is not None and int(visible.group(1)) >= 5000
        down = re.search(r"DOWN_INTERVAL_MS\s*=\s*(\d+)", js)
        assert down is not None and int(down.group(1)) >= 5000
        max_down = re.search(r"MAX_DOWN_INTERVAL_MS\s*=\s*(\d+)", js)
        assert max_down is not None and int(max_down.group(1)) >= int(down.group(1))
        assert js.count("location.reload()") == 1
        assert "reloaded" in js
        assert "document.hidden" in js
        assert "visibilitychange" in js
        assert "data-initial-state" in js
        assert "dango-retry" in js
        assert "<script" not in js


@pytest.mark.unit
class TestHealthRebridge:
    def test_health_401_rebridge_still_recovers_without_injection(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers.get("cookie", ""))
            if len(calls) == 1:
                return httpx.Response(401, content=b"nope")
            return httpx.Response(200, content=b'{"status":"ok"}')

        client = _make_client(tmp_path, handler)
        client.cookies.set("metabase.SESSION", "stale")
        with (
            patch(_GET_MB_URL, return_value="http://mb:3000"),
            patch(_REBRIDGE, return_value="fresh"),
        ):
            resp = client.get("/metabase/api/health")

        assert resp.status_code == 200
        assert resp.content == b'{"status":"ok"}'
        assert resp.cookies["metabase.SESSION"] == "fresh"
