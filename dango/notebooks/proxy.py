"""dango/notebooks/proxy.py

HTTP and WebSocket reverse proxy utilities for Marimo notebook server.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import WebSocket
from fastapi.responses import Response

logger = logging.getLogger(__name__)

# Hop-by-hop headers that must not be forwarded
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-encoding",
        "content-length",
    }
)


def _build_marimo_url(port: int, path: str, query: str | None = None) -> str:
    """Build a full URL targeting the local Marimo server.

    Args:
        port: Marimo server port.
        path: Request path (e.g. ``/api/health``).
        query: Optional query string.

    Returns:
        Full URL string.
    """
    url = f"http://127.0.0.1:{port}{path}"
    if query:
        url += f"?{query}"
    return url


def _ensure_marimo_running(project_root: Any) -> int:
    """Start Marimo if not running and return the port.

    Args:
        project_root: Project root path.

    Returns:
        Port number the Marimo server is listening on.

    Raises:
        RuntimeError: If Marimo could not be started.
    """
    from dango.notebooks.manager import get_marimo_status, start_marimo

    status = get_marimo_status(project_root)
    port = status["port"]
    if status["running"] and port is not None:
        return int(str(port))

    start_marimo(project_root)
    status = get_marimo_status(project_root)
    port = status["port"]
    if not status["running"] or port is None:
        raise RuntimeError("Marimo failed to start")
    return int(str(port))


async def proxy_to_marimo(
    request: Any,
    target_path: str,
    marimo_port: int,
) -> Response:
    """Proxy an HTTP request to the Marimo notebook server.

    Args:
        request: The incoming FastAPI ``Request``.
        target_path: Path to forward (e.g. ``/@file/notebook.py``).
        marimo_port: Port the Marimo server listens on.

    Returns:
        A FastAPI ``Response`` with the Marimo server's reply, or 502 on
        connection failure.
    """
    query = str(request.url.query) if request.url.query else None
    url = _build_marimo_url(marimo_port, target_path, query)

    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() not in _HOP_BY_HOP and key.lower() != "host":
            headers[key] = value

    body: bytes | None = None
    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()

    # Retry on connect errors — Marimo may still be starting up
    import asyncio

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
                resp = await client.request(
                    method=request.method, url=url, headers=headers, content=body
                )

            resp_headers: dict[str, str] = {}
            for key, value in resp.headers.items():
                if key.lower() not in _HOP_BY_HOP:
                    resp_headers[key] = value

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=resp_headers,
            )
        except httpx.ConnectError:
            if attempt < 2:
                await asyncio.sleep(1)
        except Exception:
            logger.error("Marimo proxy error for %s", target_path, exc_info=True)
            return Response(content="Failed to connect to Marimo", status_code=502)
    logger.error("Marimo proxy connect failed after retries for %s", target_path)
    return Response(content="Notebook server is starting, please refresh.", status_code=502)


async def proxy_websocket_to_marimo(
    websocket: WebSocket,
    target_path: str,
    marimo_port: int,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """Pipe a WebSocket connection bidirectionally to Marimo.

    Accepts the incoming WebSocket, connects to Marimo's WS endpoint,
    and relays frames in both directions until one side closes.

    Args:
        websocket: The incoming FastAPI ``WebSocket``.
        target_path: WS path on Marimo (including query string).
        marimo_port: Port the Marimo server listens on.
        extra_headers: Additional headers to forward (e.g. Marimo-Server-Token).
    """
    import websockets

    await websocket.accept()

    ws_url = f"ws://127.0.0.1:{marimo_port}{target_path}"

    try:
        async with websockets.connect(ws_url, additional_headers=extra_headers or {}) as marimo_ws:

            async def client_to_marimo() -> None:
                """Forward frames from the client WebSocket to Marimo."""
                try:
                    while True:
                        data = await websocket.receive_text()
                        await marimo_ws.send(data)
                except Exception:
                    pass

            async def marimo_to_client() -> None:
                """Forward frames from Marimo back to the client WebSocket."""
                try:
                    async for message in marimo_ws:
                        if isinstance(message, str):
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)
                except Exception:
                    pass

            tasks = [
                asyncio.create_task(client_to_marimo()),
                asyncio.create_task(marimo_to_client()),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()

    except Exception:
        logger.debug("Marimo WebSocket proxy error", exc_info=True)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
