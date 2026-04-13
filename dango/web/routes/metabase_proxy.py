"""dango/web/routes/metabase_proxy.py

Metabase reverse proxy routes with per-user session bridging.

Each Dango user's Metabase session cookie (``metabase.SESSION``) is stored
on the browser.  When a proxied request receives a 401 from Metabase, the
proxy transparently re-bridges by decrypting the user's Metabase password
and creating a new Metabase session.
"""

import logging
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Request
from fastapi.responses import Response

from dango.web.middleware.auth import is_secure_request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["metabase-proxy"])

_MB_SESSION_COOKIE = "metabase.SESSION"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_metabase_url(project_root: Path) -> str:
    """Read the Metabase URL from ``.dango/metabase.yml``, defaulting to localhost."""
    path = project_root / ".dango" / "metabase.yml"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data: dict[str, Any] = yaml.safe_load(f)
            url = data.get("metabase_url")
            if url:
                return str(url)
        except Exception:
            logger.debug("metabase_proxy_url_load_failed", exc_info=True)
    return "http://localhost:3000"


def _get_project_root(request: Request) -> Path:
    """Resolve project root from app state."""
    from dango.web.helpers import get_project_root

    root: Path | None = getattr(request.app.state, "project_root", None)
    if root is not None:
        return root
    return get_project_root()


def _get_user_mb_session(request: Request) -> str | None:
    """Extract the user's ``metabase.SESSION`` cookie from the request."""
    return request.cookies.get(_MB_SESSION_COOKIE)


async def _rebridge_if_needed(request: Request, metabase_url: str) -> str | None:
    """Create a fresh Metabase session for the current user.

    Called when a proxied request has no session cookie or gets a 401/403
    from Metabase.  Returns the new Metabase session ID, or ``None`` if
    re-bridging fails.  Passes ``db_path`` so that the bridge can
    lazy-sync users who were never synced to Metabase.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        return None
    try:
        from dango.auth.admin import get_auth_db_path
        from dango.auth.metabase_bridge import bridge_metabase_login

        project_root = _get_project_root(request)
        db_path = get_auth_db_path(project_root)
        return await bridge_metabase_login(user, project_root, metabase_url, db_path=db_path)
    except Exception:
        logger.warning("metabase_rebridge_failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Core proxy function
# ---------------------------------------------------------------------------


async def proxy_to_metabase(
    request: Request,
    target_path: str,
    session_id: str | None = None,
    metabase_url: str | None = None,
) -> Response:
    """Proxy a request to Metabase with per-user session bridging.

    Args:
        request: The incoming FastAPI request
        target_path: The path to proxy to on Metabase (e.g., "/api/health")
        session_id: Optional Metabase session ID for auth
        metabase_url: Optional Metabase base URL

    Returns:
        Response from Metabase
    """
    project_root = _get_project_root(request)
    if metabase_url is None:
        metabase_url = _get_metabase_url(project_root)

    target_url = f"{metabase_url}{target_path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    logger.debug("Proxying to Metabase: %s %s", request.method, target_url)

    # Prepare headers
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() not in ("host", "connection", "content-length"):
            headers[key] = value

    # Get request body if present
    body: bytes | None = None
    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()

    try:
        response = await _do_proxy(target_url, request.method, headers, body, session_id)

        # Re-bridge on 401 (session expired) or 403 (stale/mismatched session)
        if response.status_code in (401, 403) and session_id is not None:
            new_session = await _rebridge_if_needed(request, metabase_url)
            if new_session is not None:
                response = await _do_proxy(target_url, request.method, headers, body, new_session)
                if response.status_code not in (401, 403):
                    # Attach the new session cookie to the response
                    final = _build_response(response)
                    final.set_cookie(
                        key=_MB_SESSION_COOKIE,
                        value=new_session,
                        path="/",
                        httponly=True,
                        samesite="lax",
                        secure=is_secure_request(request.scope),
                    )
                    return final

        return _build_response(response)

    except Exception:
        logger.error("Metabase proxy error for %s", target_path, exc_info=True)
        return Response(content="Failed to connect to Metabase", status_code=502)


async def _do_proxy(
    url: str,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
    session_id: str | None,
) -> httpx.Response:
    """Execute the actual HTTP request to Metabase."""
    proxy_headers = dict(headers)
    if session_id:
        # Strip any existing metabase.SESSION from forwarded cookies to avoid duplicates
        existing_cookies = proxy_headers.get("cookie", "")
        if existing_cookies:
            parts = [
                c.strip()
                for c in existing_cookies.split(";")
                if not c.strip().startswith(f"{_MB_SESSION_COOKIE}=")
            ]
            filtered = "; ".join(parts)
            if filtered:
                proxy_headers["cookie"] = f"{filtered}; {_MB_SESSION_COOKIE}={session_id}"
            else:
                proxy_headers["cookie"] = f"{_MB_SESSION_COOKIE}={session_id}"
        else:
            proxy_headers["cookie"] = f"{_MB_SESSION_COOKIE}={session_id}"

    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        return await client.request(method=method, url=url, headers=proxy_headers, content=body)


_SKIP_HEADERS = frozenset(("content-encoding", "transfer-encoding", "content-length"))


def _build_response(proxy_response: httpx.Response) -> Response:
    """Build a FastAPI Response from an httpx response.

    Uses ``multi_items()`` so that multiple ``Set-Cookie`` headers from
    Metabase are preserved (a plain dict would keep only the last one).
    """
    response_headers: dict[str, str] = {}
    set_cookie_values: list[str] = []

    for key, value in proxy_response.headers.multi_items():
        lower = key.lower()
        if lower in _SKIP_HEADERS:
            continue
        if lower == "set-cookie":
            set_cookie_values.append(value)
        else:
            response_headers[key] = value

    response = Response(
        content=proxy_response.content,
        status_code=proxy_response.status_code,
        headers=response_headers,
    )

    for cookie_value in set_cookie_values:
        response.headers.append("set-cookie", cookie_value)

    return response


# ==============================================================================
# Metabase-specific proxy routes
# ==============================================================================


@router.api_route("/api/health", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_api_health(request: Request) -> Response:
    """Proxy Metabase health check API."""
    session_id = _get_user_mb_session(request)
    return await proxy_to_metabase(request, "/api/health", session_id)


@router.api_route("/api/session", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_api_session(request: Request) -> Response:
    """Proxy Metabase session API."""
    return await proxy_to_metabase(request, "/api/session")


@router.api_route("/api/user", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_api_user(request: Request) -> Response:
    """Proxy Metabase user API."""
    session_id = _get_user_mb_session(request)
    return await proxy_to_metabase(request, "/api/user", session_id)


@router.api_route("/api/database", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@router.api_route(
    "/api/database/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
)
async def metabase_api_database(request: Request, path: str = "") -> Response:
    """Proxy Metabase database API."""
    session_id = _get_user_mb_session(request)
    target_path = f"/api/database/{path}" if path else "/api/database"
    return await proxy_to_metabase(request, target_path, session_id)


@router.api_route("/app/{path:path}", methods=["GET"])
async def metabase_app_assets(request: Request, path: str) -> Response:
    """Proxy Metabase app assets (JS, CSS, images, etc.)."""
    return await proxy_to_metabase(request, f"/app/{path}")


@router.api_route("/public/{path:path}", methods=["GET"])
async def metabase_public_assets(request: Request, path: str) -> Response:
    """Proxy Metabase public assets."""
    return await proxy_to_metabase(request, f"/public/{path}")


@router.get("/styles.css")
async def metabase_styles(request: Request) -> Response:
    """Proxy Metabase styles.css."""
    return await proxy_to_metabase(request, "/styles.css")


@router.api_route(
    "/metabase/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
)
@router.api_route("/metabase", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_proxy(request: Request, path: str = "") -> Response:
    """Reverse proxy for Metabase with per-user session bridging.

    Routes all requests to the configured Metabase URL and uses the
    user's ``metabase.SESSION`` cookie for authentication.  On 401/403,
    attempts to re-bridge the session transparently.

    When the user is authenticated to Dango but has no ``metabase.SESSION``
    cookie, auto-bridges before proxying (instead of waiting for a 401
    that may never come — Metabase returns its login page as 200).
    """
    project_root = _get_project_root(request)
    metabase_url = _get_metabase_url(project_root)

    target_path = f"/{path}" if path else "/"
    session_id = _get_user_mb_session(request)

    # Auto-bridge: user is authenticated but has no Metabase session cookie
    if session_id is None:
        user = getattr(request.state, "user", None)
        if user is not None:
            new_session = await _rebridge_if_needed(request, metabase_url)
            if new_session is not None:
                session_id = new_session
                response = await proxy_to_metabase(request, target_path, session_id, metabase_url)
                response.set_cookie(
                    key=_MB_SESSION_COOKIE,
                    value=new_session,
                    path="/",
                    httponly=True,
                    samesite="lax",
                    secure=is_secure_request(request.scope),
                )
                return response

    return await proxy_to_metabase(request, target_path, session_id, metabase_url)
