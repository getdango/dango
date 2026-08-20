"""dango/web/routes/variables.py

Web UI for environment variable and OAuth token management (admin-only).
Provides read-only view of .env variables (masked) and OAuth token status.
Add/edit/delete for env vars only; OAuth disconnect remains on Secrets page.
"""

from __future__ import annotations

import asyncio
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
from dango.utils.env_file import serialize_env_file
from dango.web.routes.secrets import read_env_file

router = APIRouter(tags=["variables"])
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_client_ip(request: Request) -> str | None:
    """Extract client IP address from the request."""
    if request.client is not None:
        return request.client.host
    return None


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


def _mask_value(value: str) -> str:
    """Mask a secret value, showing last 4 chars only.

    For values <= 4 chars, return fully masked "****".
    """
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def _derive_oauth_status(cred: Any, result: Any) -> str:
    """Derive OAuth token status from credential and validation result.

    Args:
        cred: OAuthCredential with is_expired(), is_expiring_soon()
        result: TokenValidationResult with valid (bool), error_code (str)

    Returns:
        "unknown" (network error), "expired", "expiring_soon", or "ok"
    """
    if result.error_code == "network_error":
        return "unknown"
    if cred.is_expired() or not result.valid:
        return "expired"
    if cred.is_expiring_soon(days=7):
        return "expiring_soon"
    return "ok"


# ---------------------------------------------------------------------------
# GET /api/variables — list all env vars (masked)
# ---------------------------------------------------------------------------


@router.get("/api/variables")
async def list_variables(
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> JSONResponse:
    """List all environment variables (masked)."""
    project_root: Path = request.app.state.project_root
    env_vars = read_env_file(project_root)

    variables = [{"key": k, "masked_value": _mask_value(v)} for k, v in env_vars.items()]

    return JSONResponse(content={"variables": variables})


# ---------------------------------------------------------------------------
# POST /api/variables — add/update an env var
# ---------------------------------------------------------------------------


@router.post("/api/variables")
async def set_variable(
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
        details={"key": key, "action": action, "source_page": "variables"},
    )

    return JSONResponse(
        status_code=200,
        content={"message": f"Environment variable {action}.", "key": key},
    )


# ---------------------------------------------------------------------------
# DELETE /api/variables/{key} — remove an env var
# ---------------------------------------------------------------------------


@router.delete("/api/variables/{key}")
async def delete_variable(
    key: str,
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> JSONResponse:
    """Delete an environment variable."""
    project_root: Path = request.app.state.project_root
    env_vars = read_env_file(project_root)

    if key not in env_vars:
        return JSONResponse(status_code=404, content={"message": "Variable not found."})

    del env_vars[key]
    _write_env_file(project_root, env_vars)

    log_auth_event(
        AuditEvent.SECRET_DELETED,
        user_id=user.id,
        email=user.email,
        ip=_get_client_ip(request),
        details={"key": key, "source_page": "variables"},
    )

    return JSONResponse(status_code=200, content={"message": "Environment variable deleted."})


# ---------------------------------------------------------------------------
# GET /api/variables/oauth — OAuth token status (read-only)
# ---------------------------------------------------------------------------


@router.get("/api/variables/oauth")
async def list_oauth_status(
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> JSONResponse:
    """List OAuth token status for all connected accounts (read-only)."""
    project_root: Path = request.app.state.project_root
    oauth_items: list[dict[str, Any]] = []

    try:
        from dango.oauth.storage import OAuthStorage
        from dango.oauth.validation import validate_token

        storage = OAuthStorage(project_root)
        creds_list = storage.list()

        # Validate all credentials in parallel
        results = await asyncio.gather(
            *[asyncio.to_thread(validate_token, cred) for cred in creds_list],
            return_exceptions=True,
        )

        for cred, result in zip(creds_list, results, strict=True):
            # Handle exceptions from asyncio.to_thread
            if isinstance(result, Exception):
                logger.warning("oauth_validation_failed", exc_info=result)
                status = "unknown"
            else:
                status = _derive_oauth_status(cred, result)

            oauth_items.append(
                {
                    "source_type": cred.source_type,
                    "provider": cred.provider,
                    "identifier": cred.identifier,
                    "account_info": cred.account_info,
                    "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
                    "days_until_expiry": cred.days_until_expiry(),
                    "status": status,
                }
            )
    except Exception:
        logger.warning("oauth_list_failed", exc_info=True)

    return JSONResponse(content={"oauth_credentials": oauth_items})


# ---------------------------------------------------------------------------
# GET /settings/variables — HTML page
# ---------------------------------------------------------------------------


@router.get("/settings/variables")
async def variables_page(request: Request) -> HTMLResponse:
    """Serve the variables management page (admin-only)."""
    _user: User = request.state.user  # type: ignore
    if _user.role.value != "admin":
        return HTMLResponse(status_code=403, content="Access denied")

    from dango.web.routes.ui import _render_template

    return _render_template(
        request,
        "variables.html",
        {
            "version": dango.__version__,
            "current_page": "settings",
            "subtitle": "Variables",
        },
    )
