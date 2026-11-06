"""tests/unit/test_ws_cap.py

/ws connection cap is reserved before accept() and also applied per identity.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from starlette.websockets import WebSocketDisconnect

from dango.auth.models import Role, User
from dango.web.routes import websocket as ws_module


class _FakeWebSocket:
    """Minimal WebSocket double: accept() yields to the loop, receive blocks until released."""

    def __init__(self, user: User | None, release: asyncio.Event) -> None:
        self.scope: dict[str, Any] = {"type": "websocket", "state": {"user": user}}
        self._release = release
        self.accepted = False
        self.close_code: int | None = None

    async def accept(self) -> None:
        for _ in range(3):
            await asyncio.sleep(0)
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.close_code = code

    async def send_json(self, message: dict[str, Any]) -> None:
        return None

    async def receive_text(self) -> str:
        await self._release.wait()
        raise WebSocketDisconnect(code=1000)


def _user(n: int) -> User:
    return User(id=f"user-{n}", email=f"u{n}@example.com", role=Role.VIEWER)


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> ws_module.ConnectionManager:
    fresh = ws_module.ConnectionManager()
    monkeypatch.setattr(ws_module, "ws_manager", fresh)
    return fresh


async def _open_concurrently(
    sockets: list[_FakeWebSocket], release: asyncio.Event, manager: ws_module.ConnectionManager
) -> int:
    tasks = [asyncio.create_task(ws_module.websocket_endpoint(cast(Any, ws))) for ws in sockets]
    for _ in range(20):
        await asyncio.sleep(0)
    peak = len(manager.active_connections)
    release.set()
    await asyncio.gather(*tasks)
    return peak


@pytest.mark.unit
def test_concurrent_handshakes_never_exceed_cap(manager: ws_module.ConnectionManager) -> None:
    cap = ws_module.MAX_WS_CONNECTIONS

    async def run() -> tuple[int, list[_FakeWebSocket]]:
        release = asyncio.Event()
        sockets = [_FakeWebSocket(_user(i), release) for i in range(cap + 50)]
        return await _open_concurrently(sockets, release, manager), sockets

    peak, sockets = asyncio.run(run())

    assert peak <= cap
    assert sum(ws.accepted for ws in sockets) <= cap
    assert sum(ws.close_code == 1013 for ws in sockets) >= 50
    assert manager.active_connections == []


@pytest.mark.unit
def test_per_identity_cap(manager: ws_module.ConnectionManager) -> None:
    per_identity = getattr(ws_module, "MAX_WS_CONNECTIONS_PER_IDENTITY", 20)

    async def run() -> tuple[list[_FakeWebSocket], _FakeWebSocket]:
        release = asyncio.Event()
        same = [_FakeWebSocket(_user(1), release) for _ in range(per_identity + 1)]
        other = _FakeWebSocket(_user(2), release)
        await _open_concurrently([*same, other], release, manager)
        return same, other

    same, other = asyncio.run(run())

    assert sum(ws.accepted for ws in same) == per_identity
    assert sum(ws.close_code == 1013 for ws in same) == 1
    assert other.accepted


@pytest.mark.unit
def test_slot_released_when_accept_fails(manager: ws_module.ConnectionManager) -> None:
    class _BrokenAccept(_FakeWebSocket):
        async def accept(self) -> None:
            raise RuntimeError("handshake failed")

    async def run() -> None:
        release = asyncio.Event()
        release.set()
        with pytest.raises(RuntimeError):
            await ws_module.websocket_endpoint(cast(Any, _BrokenAccept(_user(1), release)))

    asyncio.run(run())

    assert manager.active_connections == []
    assert getattr(manager, "_pending", 0) == 0
    assert not getattr(manager, "_per_identity", {})
