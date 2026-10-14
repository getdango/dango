"""tests/unit/test_ws_auth.py

WebSocket scopes are authenticated by AuthMiddleware, origin-checked, and /ws connections are capped.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import iter_route_contexts
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from dango.web.routes import websocket as ws_module
from dango.web.routes.websocket import router as ws_router
from tests.factories.auth_app_factories import AuthProject, build_client


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    monkeypatch.setattr(ws_module.ws_manager, "active_connections", [])
    p = AuthProject(tmp_path)
    p.create_db()
    p.write_toggle("enabled: true\n")
    return p


def _connect_and_read(client: Any, path: str = "/ws", **kwargs: Any) -> dict[str, Any]:
    with client.websocket_connect(path, **kwargs) as ws:
        return ws.receive_json()


def _assert_refused(client: Any, code: int, path: str = "/ws", **kwargs: Any) -> None:
    """The server closes with *code* before sending anything."""
    received: list[dict[str, Any]] = []
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(path, **kwargs) as ws:
            received.append(ws.receive_json())
    assert received == []
    assert excinfo.value.code == code


@pytest.mark.unit
class TestWebSocketAuth:
    def test_ws_anonymous_refused(self, project: AuthProject) -> None:
        client = build_client(project.root, ws_router)

        _assert_refused(client, 1008)
        assert ws_module.ws_manager.active_connections == []

    def test_ws_viewer_session_connects(self, project: AuthProject) -> None:
        cookie = project.session_cookie(project.add_user())
        client = build_client(project.root, ws_router)

        msg = _connect_and_read(client, headers={"cookie": cookie})

        assert msg["event"] == "connected"

    def test_ws_api_key_connects(self, project: AuthProject) -> None:
        key = project.api_key(project.add_user())
        client = build_client(project.root, ws_router)

        msg = _connect_and_read(client, headers={"authorization": f"Bearer {key}"})

        assert msg["event"] == "connected"

    def test_ws_invalid_cookie_refused(self, project: AuthProject) -> None:
        client = build_client(project.root, ws_router)

        _assert_refused(client, 1008, headers={"cookie": "dango_session=not-a-real-token"})

    def test_ws_auth_disabled_unchanged(self, project: AuthProject) -> None:
        project.write_toggle("enabled: false\n")
        client = build_client(project.root, ws_router)

        assert _connect_and_read(client)["event"] == "connected"


@pytest.mark.unit
class TestWebSocketOrigin:
    def test_ws_cross_origin_refused(self, project: AuthProject) -> None:
        cookie = project.session_cookie(project.add_user())
        client = build_client(project.root, ws_router, base_url="http://dango.example")

        _assert_refused(client, 1008, headers={"cookie": cookie, "origin": "https://evil.example"})

    def test_ws_same_origin_connects(self, project: AuthProject) -> None:
        cookie = project.session_cookie(project.add_user())
        client = build_client(project.root, ws_router, base_url="http://dango.example")

        msg = _connect_and_read(
            client, headers={"cookie": cookie, "origin": "http://dango.example"}
        )

        assert msg["event"] == "connected"

    def test_ws_localhost_alias_same_port_connects(self, project: AuthProject) -> None:
        cookie = project.session_cookie(project.add_user())
        client = build_client(project.root, ws_router, base_url="http://127.0.0.1:8800")

        msg = _connect_and_read(
            client, headers={"cookie": cookie, "origin": "http://localhost:8800"}
        )

        assert msg["event"] == "connected"

    def test_ws_cross_origin_refused_even_when_auth_disabled(self, project: AuthProject) -> None:
        project.write_toggle("enabled: false\n")
        client = build_client(project.root, ws_router, base_url="http://127.0.0.1:8800")

        _assert_refused(client, 1008, headers={"origin": "https://evil.example"})


@pytest.mark.unit
class TestWebSocketCap:
    def test_ws_connection_cap(self, project: AuthProject, monkeypatch: pytest.MonkeyPatch) -> None:
        cookie = project.session_cookie(project.add_user())
        client = build_client(project.root, ws_router)
        cap = getattr(ws_module, "MAX_WS_CONNECTIONS", 100)
        monkeypatch.setattr(ws_module.ws_manager, "active_connections", [object()] * cap)

        _assert_refused(client, 1013, headers={"cookie": cookie})
        assert len(ws_module.ws_manager.active_connections) == cap

    def test_ws_below_cap_connects(
        self, project: AuthProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cookie = project.session_cookie(project.add_user())
        client = build_client(project.root, ws_router)
        monkeypatch.setattr(ws_module.ws_manager, "active_connections", [object()] * 99)

        assert _connect_and_read(client, headers={"cookie": cookie})["event"] == "connected"


@pytest.mark.unit
def test_every_websocket_route_requires_auth(project: AuthProject) -> None:
    from dango.web.app import app as dango_app

    ws_paths = [
        ctx.route.path
        for ctx in iter_route_contexts(dango_app.routes)
        if isinstance(ctx.route, WebSocketRoute)
    ]
    assert "/ws" in ws_paths

    client = build_client(project.root, dango_app.router)
    for path in ws_paths:
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        _assert_refused(client, 1008, path=concrete)
