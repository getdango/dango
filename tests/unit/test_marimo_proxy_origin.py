"""tests/unit/test_marimo_proxy_origin.py

State-changing requests under /notebooks/marimo/ must be same-origin (no blanket CSRF exemption).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient

from dango.auth.models import Role
from tests.factories.auth_app_factories import AuthProject, build_client

MARIMO_PATH = "/notebooks/marimo/api/kernel/run"
METHODS = ["POST", "PUT", "PATCH", "DELETE"]


class _FakeProxy:
    """Stand-in for the Marimo proxy route; records every request that reaches it."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.router = APIRouter()

        @self.router.api_route(
            "/notebooks/marimo/{path:path}",
            methods=["GET", "HEAD", "OPTIONS", *METHODS],
        )
        async def _proxy(request: Request, path: str) -> dict[str, str]:
            self.calls.append(request.method)
            return {"proxied": path}


@pytest.fixture
def proxy() -> _FakeProxy:
    return _FakeProxy()


@pytest.fixture
def project(tmp_path: Path) -> AuthProject:
    proj = AuthProject(tmp_path)
    proj.write_toggle("enabled: true\n")
    proj.create_db()
    return proj


@pytest.fixture
def client(project: AuthProject, proxy: _FakeProxy) -> TestClient:
    return build_client(project.root, proxy.router)


@pytest.fixture
def cookie(project: AuthProject) -> dict[str, str]:
    return {"Cookie": project.session_cookie(project.add_user(role=Role.ADMIN))}


@pytest.mark.parametrize("method", METHODS)
def test_marimo_state_change_cross_origin_refused(
    client: TestClient, proxy: _FakeProxy, cookie: dict[str, str], method: str
) -> None:
    resp = client.request(method, MARIMO_PATH, headers={**cookie, "Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "DANGO-S002"
    assert proxy.calls == []


def test_marimo_post_sibling_localhost_port_refused(
    client: TestClient, proxy: _FakeProxy, cookie: dict[str, str]
) -> None:
    resp = client.post(MARIMO_PATH, headers={**cookie, "Origin": "http://localhost:9999"})
    assert resp.status_code == 403
    assert proxy.calls == []


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("https://d.example", "d.example"),  # Caddy domain shape
        ("http://localhost:8800", "localhost:8800"),  # localhost shape
        ("http://127.0.0.1:8800", "localhost:8800"),  # loopback alias
    ],
)
def test_marimo_post_same_origin_proxied(
    client: TestClient,
    proxy: _FakeProxy,
    cookie: dict[str, str],
    origin: str,
    host: str,
) -> None:
    resp = client.post(MARIMO_PATH, headers={**cookie, "Origin": origin, "Host": host})
    assert resp.status_code == 200
    assert proxy.calls == ["POST"]


def test_marimo_post_without_origin_unchanged(
    client: TestClient, proxy: _FakeProxy, cookie: dict[str, str]
) -> None:
    assert client.post(MARIMO_PATH, headers=cookie).status_code == 200
    assert proxy.calls == ["POST"]

    anonymous = client.post(MARIMO_PATH)
    assert anonymous.status_code == 401
    assert proxy.calls == ["POST"]


def test_marimo_get_cross_origin_unchanged(
    client: TestClient, proxy: _FakeProxy, cookie: dict[str, str]
) -> None:
    resp = client.get(MARIMO_PATH, headers={**cookie, "Origin": "https://evil.example"})
    assert resp.status_code == 200
    assert proxy.calls == ["GET"]


def test_marimo_post_origin_null_refused(
    client: TestClient, proxy: _FakeProxy, cookie: dict[str, str]
) -> None:
    resp = client.post(MARIMO_PATH, headers={**cookie, "Origin": "null"})
    assert resp.status_code == 403
    assert proxy.calls == []


def test_marimo_cross_origin_api_key_not_ambient(
    client: TestClient, project: AuthProject, proxy: _FakeProxy
) -> None:
    """An explicit Bearer key is not ambient browser credentials: not subject to the check."""
    key = project.api_key(project.add_user("k@example.com", Role.ADMIN))
    resp = client.post(
        MARIMO_PATH,
        headers={"Authorization": f"Bearer {key}", "Origin": "https://evil.example"},
    )
    assert resp.status_code == 200
    assert proxy.calls == ["POST"]


def test_non_marimo_path_still_needs_csrf_header(
    client: TestClient, cookie: dict[str, str]
) -> None:
    """The new check is scoped to /notebooks/marimo/; other routes keep the CSRF header rule."""
    resp = client.post("/api/probe", headers={**cookie, "Origin": "http://localhost:8800"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Caddy forward_auth target for the Marimo WebSocket (/api/notebooks/ws-auth)
# ---------------------------------------------------------------------------

WS_AUTH = "/api/notebooks/ws-auth"


@pytest.fixture
def ws_client(project: AuthProject) -> TestClient:
    from dango.web.routes.notebooks import router

    return build_client(project.root, router)


@pytest.fixture
def editor_cookie(project: AuthProject) -> dict[str, str]:
    return {"Cookie": project.session_cookie(project.add_user("e@example.com", Role.EDITOR))}


@pytest.mark.parametrize(
    "origin", ["https://evil.example", "http://localhost:9999", "null", "https://d.example"]
)
def test_ws_auth_cross_origin_refused(
    ws_client: TestClient, editor_cookie: dict[str, str], origin: str
) -> None:
    resp = ws_client.get(WS_AUTH, headers={**editor_cookie, "Origin": origin, "Host": "x.example"})
    assert resp.status_code == 403


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("https://d.example", "d.example"),  # Caddy domain shape
        ("http://localhost:8800", "localhost:8800"),  # localhost shape
        ("http://203.0.113.5", "203.0.113.5"),  # :80 mode by IP
    ],
)
def test_ws_auth_same_origin_allowed(
    ws_client: TestClient, editor_cookie: dict[str, str], origin: str, host: str
) -> None:
    resp = ws_client.get(WS_AUTH, headers={**editor_cookie, "Origin": origin, "Host": host})
    assert resp.status_code == 200


def test_ws_auth_missing_origin_refused(
    ws_client: TestClient, editor_cookie: dict[str, str]
) -> None:
    assert ws_client.get(WS_AUTH, headers=editor_cookie).status_code == 403


def test_ws_auth_x_forwarded_host_not_trusted(
    ws_client: TestClient, editor_cookie: dict[str, str]
) -> None:
    headers = {
        **editor_cookie,
        "Origin": "https://evil.example",
        "Host": "d.example",
        "X-Forwarded-Host": "evil.example",
    }
    assert ws_client.get(WS_AUTH, headers=headers).status_code == 403


@pytest.mark.parametrize("path", ["/notebooks/marimo/terminal/ws", "/notebooks/marimo/ws"])
def test_dango_web_does_not_proxy_marimo_websockets(
    ws_client: TestClient, editor_cookie: dict[str, str], path: str
) -> None:
    """Marimo WebSockets are never forwarded by dango-web (cloud: only Caddy's /ws handle)."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect(
            path, headers={**editor_cookie, "Origin": "http://localhost:8800"}
        ):
            pass
