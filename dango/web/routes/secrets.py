"""dango/web/routes/secrets.py

Secrets and OAuth credential management API for the admin dashboard.

Provides CRUD for environment variables (stored in ``.env`` on the server)
and read/delete for OAuth credentials (stored in ``.dlt/secrets.toml``).
All endpoints require ``config.manage`` permission (admin-only).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import dango
from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.logging import get_logger
from dango.utils.env_file import parse_env_file, serialize_env_file

router = APIRouter(tags=["secrets"])
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_client_ip(request: Request) -> str | None:
    """Extract client IP address from the request."""
    if request.client is not None:
        return request.client.host
    return None


def read_env_file(project_root: Path) -> dict[str, str]:
    """Read and parse the project ``.env`` file."""
    env_file = project_root / ".env"
    if not env_file.exists():
        return {}
    return parse_env_file(env_file.read_text(encoding="utf-8"))


def _write_env_file(project_root: Path, env_vars: dict[str, str]) -> None:
    """Write env vars to the project ``.env`` file with mode 0o600."""
    env_file = project_root / ".env"
    content = serialize_env_file(env_vars)
    # Create with restrictive permissions to avoid TOCTOU window
    fd = os.open(str(env_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# GET /api/secrets — list all secrets (masked)
# ---------------------------------------------------------------------------


@router.get("/api/secrets")
async def list_secrets(
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> JSONResponse:
    """List all environment variables and OAuth credentials (masked)."""
    project_root: Path = request.app.state.project_root

    # Env vars
    env_vars = read_env_file(project_root)
    env_items: list[dict[str, str]] = [
        {"key": k, "masked_value": "***", "source": "env"} for k in env_vars
    ]

    # OAuth credentials
    oauth_items: list[dict[str, Any]] = []
    try:
        from dango.oauth.storage import OAuthStorage

        storage = OAuthStorage(project_root)
        for cred in storage.list():
            oauth_items.append(
                {
                    "source_type": cred.source_type,
                    "provider": cred.provider,
                    "identifier": cred.identifier,
                    "connected_at": cred.created_at.isoformat(),
                    "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
                    "source": "oauth",
                }
            )
    except Exception:
        logger.warning("secrets_oauth_list_failed", exc_info=True)

    return JSONResponse(content={"env_vars": env_items, "oauth_credentials": oauth_items})


# ---------------------------------------------------------------------------
# POST /api/secrets — add/update an env var
# ---------------------------------------------------------------------------


@router.post("/api/secrets")
async def set_secret(
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> JSONResponse:
    """Add or update an environment variable."""
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Invalid JSON body."})

    key = body.get("key", "").strip()
    value = body.get("value", "")
    if not key:
        return JSONResponse(status_code=400, content={"message": "Key is required."})
    if not isinstance(value, str):
        return JSONResponse(status_code=400, content={"message": "Value must be a string."})

    project_root: Path = request.app.state.project_root
    env_vars = read_env_file(project_root)
    action = "updated" if key in env_vars else "created"
    env_vars[key] = value
    _write_env_file(project_root, env_vars)

    log_auth_event(
        AuditEvent.SECRET_SET,
        user_id=user.id,
        email=user.email,
        ip=_get_client_ip(request),
        details={"key": key, "action": action},
    )

    return JSONResponse(
        status_code=200,
        content={"message": f"Environment variable {action}.", "key": key},
    )


# ---------------------------------------------------------------------------
# DELETE /api/secrets/{key} — remove an env var
# ---------------------------------------------------------------------------


@router.delete("/api/secrets/{key}")
async def delete_secret(
    key: str,
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> JSONResponse:
    """Delete an environment variable."""
    project_root: Path = request.app.state.project_root
    env_vars = read_env_file(project_root)

    if key not in env_vars:
        return JSONResponse(status_code=404, content={"message": f"Key '{key}' not found."})

    del env_vars[key]
    _write_env_file(project_root, env_vars)

    log_auth_event(
        AuditEvent.SECRET_DELETED,
        user_id=user.id,
        email=user.email,
        ip=_get_client_ip(request),
        details={"key": key},
    )

    return JSONResponse(status_code=200, content={"message": f"Deleted '{key}'."})


# ---------------------------------------------------------------------------
# GET /api/secrets/oauth — list OAuth credential status
# ---------------------------------------------------------------------------


@router.get("/api/secrets/oauth")
async def list_oauth_credentials(
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> JSONResponse:
    """List OAuth credential status for connected data sources."""
    project_root: Path = request.app.state.project_root
    items: list[dict[str, Any]] = []

    try:
        from dango.oauth.storage import OAuthStorage

        storage = OAuthStorage(project_root)
        for cred in storage.list():
            items.append(
                {
                    "source_type": cred.source_type,
                    "provider": cred.provider,
                    "identifier": cred.identifier,
                    "account_info": cred.account_info,
                    "connected_at": cred.created_at.isoformat(),
                    "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
                    "is_expired": cred.is_expired(),
                    "days_until_expiry": cred.days_until_expiry(),
                }
            )
    except Exception:
        logger.warning("secrets_oauth_list_failed", exc_info=True)

    return JSONResponse(content=items)


# ---------------------------------------------------------------------------
# DELETE /api/secrets/oauth/{source_type} — disconnect OAuth
# ---------------------------------------------------------------------------


@router.delete("/api/secrets/oauth/{source_type}")
async def disconnect_oauth(
    source_type: str,
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> JSONResponse:
    """Disconnect OAuth credentials for a data source."""
    project_root: Path = request.app.state.project_root

    try:
        from dango.oauth.storage import OAuthStorage

        storage = OAuthStorage(project_root)
        if not storage.exists(source_type):
            return JSONResponse(
                status_code=404,
                content={"message": f"No OAuth credentials for '{source_type}'."},
            )

        storage.delete(source_type)
    except Exception:
        logger.warning("secrets_oauth_disconnect_failed", source_type=source_type, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"message": "Failed to disconnect OAuth credentials."},
        )

    log_auth_event(
        AuditEvent.OAUTH_SOURCE_DISCONNECTED,
        user_id=user.id,
        email=user.email,
        ip=_get_client_ip(request),
        details={"source_type": source_type},
    )

    return JSONResponse(
        status_code=200,
        content={"message": f"Disconnected OAuth for '{source_type}'."},
    )


# ---------------------------------------------------------------------------
# GET /settings/secrets — admin secrets page
# ---------------------------------------------------------------------------


@router.get("/settings/secrets")
async def secrets_page(
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> HTMLResponse:
    """Render the admin secrets management page."""
    from dango.web.routes.ui import _render_template

    return _render_template(
        "secrets.html",
        {
            "request": request,
            "version": dango.__version__,
            "current_page": "settings",
            "subtitle": "Secrets & Credentials",
        },
    )
