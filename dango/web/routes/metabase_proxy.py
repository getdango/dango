"""dango/web/routes/metabase_proxy.py

Metabase reverse proxy routes with SSO session management.
"""

import logging
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Request
from fastapi.responses import Response

from dango.web.helpers import get_project_root

logger = logging.getLogger(__name__)

router = APIRouter(tags=["metabase-proxy"])

# Store Metabase session for SSO
_metabase_session: dict[str, Any] = {}


async def proxy_to_metabase(request: Request, target_path: str, session_id: str = None) -> Response:
    """Proxy a request to Metabase.

    Args:
        request: The incoming FastAPI request
        target_path: The path to proxy to on Metabase (e.g., "/api/health")
        session_id: Optional Metabase session ID for auth

    Returns:
        Response from Metabase
    """
    metabase_url = "http://localhost:3000"
    target_url = f"{metabase_url}{target_path}"

    if request.url.query:
        target_url += f"?{request.url.query}"

    logger.debug(f"Proxying to Metabase: {request.method} {target_url}")

    # Prepare headers
    headers = {}
    for key, value in request.headers.items():
        if key.lower() not in ["host", "connection", "content-length"]:
            headers[key] = value

    # Add session cookie if provided
    if session_id:
        existing_cookies = headers.get("cookie", "")
        if existing_cookies:
            headers["cookie"] = f"{existing_cookies}; metabase.SESSION={session_id}"
        else:
            headers["cookie"] = f"metabase.SESSION={session_id}"

    # Get request body if present
    body = None
    if request.method in ["POST", "PUT", "PATCH"]:
        body = await request.body()

    # Make proxy request
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
            proxy_response = await client.request(
                method=request.method, url=target_url, headers=headers, content=body
            )

            # Build response headers
            response_headers = {}
            for key, value in proxy_response.headers.items():
                if key.lower() not in ["content-encoding", "transfer-encoding", "content-length"]:
                    response_headers[key] = value

            return Response(
                content=proxy_response.content,
                status_code=proxy_response.status_code,
                headers=response_headers,
            )

    except Exception as e:
        logger.error(f"Metabase proxy error for {target_path}: {e}")
        return Response(content=f"Proxy error: {str(e)}", status_code=502)


async def get_metabase_session() -> str:
    """Get or create Metabase session for auto-login.

    Returns:
        Session ID for Metabase
    """
    # Return cached session if valid
    if _metabase_session.get("id"):
        # TODO: Check if session is still valid
        return _metabase_session["id"]

    # Load credentials
    try:
        project_root = get_project_root()
        metabase_config_file = project_root / ".dango" / "metabase.yml"

        if not metabase_config_file.exists():
            logger.error("Metabase config not found")
            return None

        with open(metabase_config_file, encoding="utf-8") as f:
            metabase_config = yaml.safe_load(f)

        admin_email = metabase_config.get("admin", {}).get("email")
        admin_password = metabase_config.get("admin", {}).get("password")

        if not admin_email or not admin_password:
            logger.error("Metabase credentials not found in config")
            return None

    except Exception as e:
        logger.error(f"Failed to load Metabase config: {e}")
        return None

    # Create new session by logging in
    try:
        async with httpx.AsyncClient() as client:
            login_response = await client.post(
                "http://localhost:3000/api/session",
                json={"username": admin_email, "password": admin_password},
                timeout=10.0,
            )

            if login_response.status_code == 200:
                session_data = login_response.json()
                session_id = session_data.get("id")

                # Cache the session
                _metabase_session["id"] = session_id
                _metabase_session["email"] = admin_email

                logger.info(f"Created Metabase session: {session_id[:8]}...")
                return session_id
            else:
                logger.error(f"Metabase login failed: {login_response.status_code}")
                return None

    except Exception as e:
        logger.error(f"Error creating Metabase session: {e}")
        return None


# ==============================================================================
# Metabase-specific proxy routes
# ==============================================================================


@router.api_route("/api/health", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_api_health(request: Request):
    """Proxy Metabase health check API."""
    session_id = await get_metabase_session()
    return await proxy_to_metabase(request, "/api/health", session_id)


@router.api_route("/api/session", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_api_session(request: Request):
    """Proxy Metabase session API."""
    return await proxy_to_metabase(request, "/api/session")


@router.api_route("/api/user", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_api_user(request: Request):
    """Proxy Metabase user API."""
    session_id = await get_metabase_session()
    return await proxy_to_metabase(request, "/api/user", session_id)


@router.api_route("/api/database", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@router.api_route(
    "/api/database/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
)
async def metabase_api_database(request: Request, path: str = ""):
    """Proxy Metabase database API."""
    session_id = await get_metabase_session()
    target_path = f"/api/database/{path}" if path else "/api/database"
    return await proxy_to_metabase(request, target_path, session_id)


@router.api_route("/app/{path:path}", methods=["GET"])
async def metabase_app_assets(request: Request, path: str):
    """Proxy Metabase app assets (JS, CSS, images, etc.)."""
    return await proxy_to_metabase(request, f"/app/{path}")


@router.api_route("/public/{path:path}", methods=["GET"])
async def metabase_public_assets(request: Request, path: str):
    """Proxy Metabase public assets."""
    return await proxy_to_metabase(request, f"/public/{path}")


@router.get("/styles.css")
async def metabase_styles(request: Request):
    """Proxy Metabase styles.css."""
    return await proxy_to_metabase(request, "/styles.css")


@router.api_route(
    "/metabase/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
)
@router.api_route("/metabase", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_proxy(request: Request, path: str = ""):
    """Reverse proxy for Metabase with automatic SSO.

    Routes all requests to http://localhost:3000 and automatically
    handles authentication by injecting session cookies.
    """
    metabase_url = "http://localhost:3000"

    # Build target URL
    if path:
        target_url = f"{metabase_url}/{path}"
    else:
        target_url = metabase_url

    if request.url.query:
        target_url += f"?{request.url.query}"

    logger.info(f"Proxying: {request.method} {target_url}")

    # Get or create Metabase session
    session_id = await get_metabase_session()

    # Prepare headers
    headers = {}
    for key, value in request.headers.items():
        # Skip headers that should not be forwarded
        if key.lower() not in ["host", "connection", "content-length"]:
            headers[key] = value

    # Add session cookie if we have one
    if session_id:
        # Add to Cookie header
        existing_cookies = headers.get("cookie", "")
        if existing_cookies:
            headers["cookie"] = f"{existing_cookies}; metabase.SESSION={session_id}"
        else:
            headers["cookie"] = f"metabase.SESSION={session_id}"

    # Get request body if present
    body = None
    if request.method in ["POST", "PUT", "PATCH"]:
        body = await request.body()

    # Make proxy request
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
            proxy_response = await client.request(
                method=request.method, url=target_url, headers=headers, content=body
            )

            # Build response
            response_headers = {}
            for key, value in proxy_response.headers.items():
                # Skip headers that cause issues
                if key.lower() not in ["content-encoding", "transfer-encoding", "content-length"]:
                    response_headers[key] = value

            # Return proxy response without modification (no nav bar injection)
            content = proxy_response.content
            return Response(
                content=content, status_code=proxy_response.status_code, headers=response_headers
            )

    except Exception as e:
        logger.error(f"Proxy error: {e}")
        return Response(content=f"Proxy error: {str(e)}", status_code=502)
