"""dango/web/routes/websocket.py

WebSocket connection manager and endpoint for real-time updates.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

_MAX_WS_MESSAGE_SIZE = 4096  # 4 KB
MAX_WS_CONNECTIONS = 100
MAX_WS_CONNECTIONS_PER_IDENTITY = 20
# An authenticated connection re-checks the credential that opened it this often. Each
# check is one short SQLite read (about 1-2 ms), run off the event loop.
WS_REVALIDATE_INTERVAL_SECONDS = 10.0


def _redact_message(message: dict) -> dict:
    """Mask known secret values and secret-looking patterns in every string of *message*.

    Every connected client (any role) receives broadcasts, and sync error text can carry a
    source credential. The project's secrets are loaded when the project root is known.
    """
    from dango.utils.redact import load_secret_values, redact_nested

    try:
        from dango.web.helpers import get_project_root

        secrets = load_secret_values(get_project_root())
    except Exception:
        secrets = frozenset()
    redacted = redact_nested(message, secrets)
    return redacted if isinstance(redacted, dict) else message


class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []
        # Slots reserved by try_reserve() whose accept() has not finished yet
        self._pending: int = 0
        # Reserved + active connection count per identity
        self._per_identity: dict[str, int] = {}
        self._identity_of: dict[WebSocket, str] = {}
        # Per authenticated connection: the loop it runs on and its "re-check now" flag
        self._wake: dict[WebSocket, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}

    def try_reserve(self, identity: str) -> bool:
        """Reserve a connection slot for *identity*; False if a cap is reached.

        Synchronous (no ``await``), so concurrent handshakes cannot all pass
        the check before any of them is counted.
        """
        if len(self.active_connections) + self._pending >= MAX_WS_CONNECTIONS:
            return False
        if self._per_identity.get(identity, 0) >= MAX_WS_CONNECTIONS_PER_IDENTITY:
            return False
        self._pending += 1
        self._per_identity[identity] = self._per_identity.get(identity, 0) + 1
        return True

    def _release_identity(self, identity: str) -> None:
        remaining = self._per_identity.get(identity, 0) - 1
        if remaining > 0:
            self._per_identity[identity] = remaining
        else:
            self._per_identity.pop(identity, None)

    async def connect(self, websocket: WebSocket, identity: str | None = None) -> None:
        """Accept new WebSocket connection.

        With *identity*, the slot must have been reserved via ``try_reserve()``;
        it is released if ``accept()`` fails.
        """
        try:
            await websocket.accept()
        except BaseException:
            if identity is not None:
                self._pending -= 1
                self._release_identity(identity)
            raise
        self.active_connections.append(websocket)
        if identity is not None:
            self._pending -= 1
            self._identity_of[websocket] = identity
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")

    def _forget(self, websocket: WebSocket) -> None:
        """Drop *websocket* and release its identity slot (idempotent)."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        identity = self._identity_of.pop(websocket, None)
        if identity is not None:
            self._release_identity(identity)
        self._wake.pop(websocket, None)

    def watch(self, websocket: WebSocket) -> asyncio.Event:
        """Register *websocket* for ``request_recheck()``; call from its own task."""
        event = asyncio.Event()
        self._wake[websocket] = (asyncio.get_running_loop(), event)
        return event

    def request_recheck(self, user_id: str | None = None) -> None:
        """Ask the connections of *user_id* (all if ``None``) to re-check their credential now.

        Call after a session, API key or account was ended. Safe from any thread: it only
        schedules ``Event.set`` on each connection's own loop. The connection itself decides
        (same check as the periodic one), so a connection whose credential is still valid
        is not touched.
        """
        wanted = None if user_id is None else f"user:{user_id}"
        for websocket, (loop, event) in list(self._wake.items()):
            if wanted is not None and self._identity_of.get(websocket) != wanted:
                continue
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # that loop is already closed
                continue

    async def close_revoked(self, websocket: WebSocket) -> None:
        """Close *websocket* with 1008 because its credential is no longer valid.

        The connection is forgotten first, so no broadcast reaches it from this point on.
        Safe if the peer is already gone or the connection was already closed.
        """
        self._forget(websocket)
        logger.info(
            "WebSocket closed: credential no longer valid. Total connections: %d",
            len(self.active_connections),
        )
        try:
            await websocket.close(code=1008)
        except Exception:
            logger.debug("WebSocket was already closed", exc_info=True)

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove WebSocket connection."""
        self._forget(websocket)
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")

    async def broadcast(self, message: dict, *, log: bool = True) -> None:
        """Broadcast message to all connected WebSocket clients.

        Args:
            message: The message dict to broadcast.
            log: If True (default), also write to activity log. Callers that
                 already write their own activity log entries should pass
                 ``log=False`` to avoid duplicates.
        """
        message = _redact_message(message)
        if log:
            from dango.web.helpers import append_log_entry

            event = message.get("event", "")
            _LOG_LEVELS = {
                "sync_completed": "success",
                "sync_failed": "error",
                "dbt_run_all_completed": "success",
                "dbt_run_all_failed": "error",
            }
            level = _LOG_LEVELS.get(event, "info")
            append_log_entry(
                {
                    "timestamp": message.get(
                        "timestamp", datetime.now(tz=timezone.utc).isoformat()
                    ),
                    "level": level,
                    "source": message.get("source", "system"),
                    "message": message.get("message", event),
                }
            )

        # Broadcast to WebSocket clients
        disconnected = []
        # Iterate a snapshot: a connection may be closed (and forgotten) while a send is awaited
        for connection in list(self.active_connections):
            if connection not in self.active_connections:
                continue
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error sending to WebSocket: {e}")
                disconnected.append(connection)

        # Clean up disconnected clients
        for connection in disconnected:
            self._forget(connection)


# Global WebSocket manager
ws_manager = ConnectionManager()


def _ws_identity(websocket: WebSocket) -> str:
    """Identity for the per-identity cap: the user id set by AuthMiddleware.

    API keys count as their owner. Without a user (auth disabled), all
    connections share one local identity.
    """
    state = websocket.scope.get("state") or {}
    user = state.get("user")
    if user is not None:
        return f"user:{user.id}"
    return "local-auth-disabled" if state.get("auth_disabled") else "anonymous"


class _CredentialCheck:
    """Re-check of the credential that opened a connection (``scope["state"]["ws_revalidate"]``)."""

    def __init__(self, revalidate: Callable[[], Any]) -> None:
        self._revalidate = revalidate
        self._failing = False

    async def still_valid(self) -> bool:
        """False only when the credential was checked and is no longer valid.

        A failing check (for example a locked or unreadable auth database) keeps the
        connection: closing healthy connections because of a transient error would be worse
        than the delay. It is logged once per streak (exception type only) and retried at the
        next interval. The read runs in a worker thread so it cannot block the event loop.
        """
        try:
            user = await asyncio.to_thread(self._revalidate)
        except Exception as exc:
            if not self._failing:
                logger.warning("WebSocket credential re-check failed: %s", type(exc).__name__)
            self._failing = True
            return True
        self._failing = False
        return user is not None


def _discard(task: asyncio.Future[Any] | None) -> None:
    """Cancel *task*, or mark a finished task's outcome as seen."""
    if task is None:
        return
    if not task.done():
        task.cancel()
    elif not task.cancelled():
        task.exception()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time updates.

    Clients can connect to receive real-time updates about:
    - Sync progress and completion
    - Errors and warnings
    - Data freshness changes

    Authentication happens in ``AuthMiddleware``. Connections beyond
    ``MAX_WS_CONNECTIONS`` in total or ``MAX_WS_CONNECTIONS_PER_IDENTITY`` per
    user are refused with close code 1013 (try again later).

    A connection authenticated by a session or API key re-checks that credential every
    ``WS_REVALIDATE_INTERVAL_SECONDS`` (and at once when ``request_recheck()`` is called) and
    is closed with code 1008 when it is no longer valid. Connections with no credential to
    revoke (authentication turned off for a local request) are never re-checked.
    """
    identity = _ws_identity(websocket)
    if not ws_manager.try_reserve(identity):
        logger.warning("WebSocket connection refused: connection limit reached")
        await websocket.close(code=1013)
        return

    await ws_manager.connect(websocket, identity)

    revalidate = (websocket.scope.get("state") or {}).get("ws_revalidate")
    check = _CredentialCheck(revalidate) if revalidate is not None else None
    wake = ws_manager.watch(websocket) if check is not None else None
    receive_task: asyncio.Future[str] | None = None
    wake_task: asyncio.Future[Any] | None = None

    try:
        # Send welcome message
        await websocket.send_json(
            {
                "event": "connected",
                "message": "Connected to Dango real-time updates",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }
        )

        # Keep connection alive and listen for client messages. Reading runs as a task so the
        # wait can also end when a credential re-check is due.
        next_check = time.monotonic() + WS_REVALIDATE_INTERVAL_SECONDS
        receive_task = asyncio.ensure_future(websocket.receive_text())
        if wake is not None:
            wake_task = asyncio.ensure_future(wake.wait())
        while True:
            waiting = {t for t in (receive_task, wake_task) if t is not None}
            timeout = None if check is None else max(0.0, next_check - time.monotonic())
            done, _pending = await asyncio.wait(
                waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )

            if check is not None and wake is not None:
                woken = wake_task in done
                if woken:
                    wake.clear()
                    wake_task = asyncio.ensure_future(wake.wait())
                if woken or time.monotonic() >= next_check:
                    next_check = time.monotonic() + WS_REVALIDATE_INTERVAL_SECONDS
                    if not await check.still_valid():
                        await ws_manager.close_revoked(websocket)
                        return

            if receive_task not in done:
                continue
            data = receive_task.result()
            receive_task = asyncio.ensure_future(websocket.receive_text())

            if len(data) > _MAX_WS_MESSAGE_SIZE:
                await websocket.send_json({"event": "error", "message": "Message too large"})
                continue

            # Echo back for now (can add client commands later)
            await websocket.send_json(
                {
                    "event": "echo",
                    "data": data,
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                }
            )

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        logger.info("Client disconnected from WebSocket")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)
    finally:
        _discard(receive_task)
        _discard(wake_task)
