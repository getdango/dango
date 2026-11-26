"""tests/unit/test_ws_manager_revocation.py

ConnectionManager.close_revoked(): no further broadcast to a revoked connection, slot released, concurrent close tolerated.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from dango.web.routes import websocket as ws_module


class _Sock:
    """WebSocket double for manager-level tests."""

    def __init__(self, name: str, gate: asyncio.Event | None = None) -> None:
        self.name = name
        self.gate = gate
        self.received: list[dict[str, Any]] = []
        self.close_code: int | None = None

    async def send_json(self, message: dict[str, Any]) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.close_code is not None:
            raise RuntimeError("send after close")
        self.received.append(message)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


def _register(manager: ws_module.ConnectionManager, sock: _Sock, identity: str) -> None:
    assert manager.try_reserve(identity)
    manager.active_connections.append(cast(Any, sock))
    manager._pending -= 1
    manager._identity_of[cast(Any, sock)] = identity


@pytest.mark.unit
class TestManagerRevocation:
    def test_revoked_connection_gets_no_broadcast_and_slot_is_released(self) -> None:
        async def run() -> tuple[_Sock, _Sock, ws_module.ConnectionManager]:
            manager = ws_module.ConnectionManager()
            revoked, healthy = _Sock("revoked"), _Sock("healthy")
            _register(manager, revoked, "user:1")
            _register(manager, healthy, "user:2")

            await manager.close_revoked(cast(Any, revoked))
            await manager.broadcast({"event": "probe"}, log=False)
            return revoked, healthy, manager

        revoked, healthy, manager = asyncio.run(run())

        assert revoked.close_code == 1008
        assert revoked.received == []
        assert [m["event"] for m in healthy.received] == ["probe"]
        assert manager._per_identity == {"user:2": 1}
        assert manager.active_connections == [healthy]

    def test_connection_revoked_during_broadcast_does_not_make_others_miss_it(self) -> None:
        async def run() -> tuple[_Sock, _Sock, _Sock]:
            manager = ws_module.ConnectionManager()
            gate = asyncio.Event()
            first, second, third = _Sock("first", gate), _Sock("second"), _Sock("third")
            for i, sock in enumerate((first, second, third)):
                _register(manager, sock, f"user:{i}")

            task = asyncio.create_task(manager.broadcast({"event": "probe"}, log=False))
            await asyncio.sleep(0)  # broadcast is now waiting inside first.send_json
            await manager.close_revoked(cast(Any, first))
            gate.set()
            await task
            return first, second, third

        _first, second, third = asyncio.run(run())

        assert [m["event"] for m in second.received] == ["probe"]
        assert [m["event"] for m in third.received] == ["probe"]

    def test_closing_twice_and_closing_a_closed_socket_is_harmless(self) -> None:
        async def run() -> ws_module.ConnectionManager:
            manager = ws_module.ConnectionManager()
            sock = _Sock("a")
            _register(manager, sock, "user:1")
            await manager.close_revoked(cast(Any, sock))
            await manager.close_revoked(cast(Any, sock))
            return manager

        manager = asyncio.run(run())

        assert manager.active_connections == []
        assert manager._per_identity == {}
