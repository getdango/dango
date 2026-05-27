"""dango/web/routes/websocket.py

WebSocket connection manager and endpoint for real-time updates.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

_MAX_WS_MESSAGE_SIZE = 4096  # 4 KB


class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        """Accept new WebSocket connection."""
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove WebSocket connection."""
        self.active_connections.remove(websocket)
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
            try:
                self.active_connections.remove(connection)
            except ValueError:
                pass


# Global WebSocket manager
ws_manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time updates.

    Clients can connect to receive real-time updates about:
    - Sync progress and completion
    - Errors and warnings
    - Data freshness changes
    """
    await ws_manager.connect(websocket)

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
