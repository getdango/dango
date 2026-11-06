"""dango/web/routes/websocket.py

WebSocket connection manager and endpoint for real-time updates.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

_MAX_WS_MESSAGE_SIZE = 4096  # 4 KB
MAX_WS_CONNECTIONS = 100
MAX_WS_CONNECTIONS_PER_IDENTITY = 20


class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []
        # Slots reserved by try_reserve() whose accept() has not finished yet
        self._pending: int = 0
        # Reserved + active connection count per identity
        self._per_identity: dict[str, int] = {}
        self._identity_of: dict[WebSocket, str] = {}

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
        for connection in self.active_connections:
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
    """
    identity = _ws_identity(websocket)
    if not ws_manager.try_reserve(identity):
        logger.warning("WebSocket connection refused: connection limit reached")
        await websocket.close(code=1013)
        return

    await ws_manager.connect(websocket, identity)

    try:
        # Send welcome message
        await websocket.send_json(
            {
                "event": "connected",
                "message": "Connected to Dango real-time updates",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }
        )

        # Keep connection alive and listen for client messages
        while True:
            # Wait for messages from client (ping/pong for keepalive)
            data = await websocket.receive_text()

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
