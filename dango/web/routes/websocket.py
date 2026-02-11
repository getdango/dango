"""dango/web/routes/websocket.py

WebSocket connection manager and endpoint for real-time updates.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from dango.web.helpers import append_log_entry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        """Accept new WebSocket connection."""
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        """Remove WebSocket connection."""
        self.active_connections.remove(websocket)
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients and persist to logs."""
        # Persist to logs
        log_entry = {
            "timestamp": message.get("timestamp", datetime.now().isoformat()),
            "level": self._get_log_level(message.get("event", "")),
            "source": message.get("source", "system"),
            "message": message.get("message", str(message.get("event", ""))),
        }
        append_log_entry(log_entry)

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

    def _get_log_level(self, event: str) -> str:
        """Determine log level from event type."""
        if "completed" in event or "success" in event:
            return "success"
        elif "failed" in event or "error" in event:
            return "error"
        elif "warning" in event:
            return "warning"
        else:
            return "info"


# Global WebSocket manager
ws_manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
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
                "timestamp": datetime.now().isoformat(),
            }
        )

        # Keep connection alive and listen for client messages
        while True:
            # Wait for messages from client (ping/pong for keepalive)
            data = await websocket.receive_text()

            # Echo back for now (can add client commands later)
            await websocket.send_json(
                {"event": "echo", "data": data, "timestamp": datetime.now().isoformat()}
            )

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        logger.info("Client disconnected from WebSocket")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)
