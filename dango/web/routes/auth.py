"""dango/web/routes/auth.py

API endpoints and page routes for user authentication.

Provides login/logout, session management, API key CRUD, password changes,
and renders the login and change-password pages. All endpoints use the
auth database resolved from the application's project root.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

import dango
from dango.auth.admin import get_auth_db_path
from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.database import (
    get_session_by_token,
    get_user_by_email,
    list_user_api_keys,
    list_user_sessions,
    update_user,
)
from dango.auth.lockout import check_account_locked, record_failed_login, reset_failed_logins
from dango.auth.models import User, UserResponse, UserUpdate
from dango.auth.security import (
    check_password_strength,
    hash_password,
    hash_token,
    verify_password,
)
from dango.auth.sessions import (
    create_api_key,
    create_session,
    invalidate_all_sessions,
    invalidate_session,
    revoke_api_key,
)
from dango.logging import get_logger
from dango.web.middleware.auth import COOKIE_NAME, is_secure_request
from dango.web.models import (
    ApiKeyCreateResponse,
    ApiKeyResponse,
    ChangePasswordRequest,
    CreateApiKeyRequest,
    LoginRequest,
    SessionResponse,
)
from dango.web.routes.ui import _render_template

router = APIRouter(tags=["auth"])
logger = get_logger(__name__)

# Pre-computed dummy hash: equalizes bcrypt timing for unknown/inactive emails.
_DUMMY_PASSWORD_HASH = hash_password("timing_equalization_dummy")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_db_path(request: Request) -> Path:
    """Resolve the auth database path from the application state."""
    project_root: Path = request.app.state.project_root
    return get_auth_db_path(project_root)


def _get_current_user(request: Request) -> User | None:
    """Return the authenticated user from request state, or None."""
    return getattr(request.state, "user", None)


def _get_client_ip(request: Request) -> str | None:
    """Extract client IP address from the request."""
    if request.client is not None:
        return request.client.host
    return None


def _get_user_agent(request: Request) -> str | None:
    """Extract User-Agent header from the request."""
    return request.headers.get("user-agent")


def _set_session_cookie(response: JSONResponse, token: str, request: Request) -> None:
    """Set the session cookie on a response with appropriate security flags."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_secure_request(request.scope),
    )


def _get_auth_config(request: Request) -> Any:
    """Load AuthConfig from project config. Returns None on failure."""
    try:
        from dango.config.helpers import load_config

        project_root: Path = request.app.state.project_root
        config = load_config(project_root)
        return config.auth
    except Exception:
        logger.debug("auth_config_not_loaded", reason="no project config, using defaults")
        return None


def _get_current_token_hash(request: Request) -> str | None:
    """Hash the current session cookie to identify the active session."""
    cookie_token = request.cookies.get(COOKIE_NAME)
    if cookie_token is None:
        return None
    return hash_token(cookie_token)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@router.post("/api/auth/login")
async def login(request: Request) -> JSONResponse:
    """Authenticate a user with email and password."""
    db_path = _get_db_path(request)
    try:
        body = await request.json()
        login_data = LoginRequest(**body)
    except (ValueError, ValidationError):
        return JSONResponse(status_code=400, content={"message": "Invalid request body"})
    ip = _get_client_ip(request)

    # Load lockout config
    auth_config = _get_auth_config(request)
    max_attempts = 5
    lockout_minutes = 15
    session_max_days = 30
    if auth_config is not None:
        max_attempts = auth_config.lockout.max_attempts
        lockout_minutes = auth_config.lockout.lockout_minutes
        session_max_days = auth_config.session_max_days

    # Check lockout
    is_locked, remaining = check_account_locked(db_path, login_data.email)
    if is_locked:
        return JSONResponse(
            status_code=423,
            content={
                "message": "Account is temporarily locked",
                "remaining_seconds": remaining,
            },
        )

    # Look up user
    user = get_user_by_email(db_path, login_data.email)
    if user is None:
        # Equalize timing: run bcrypt even for unknown emails
        verify_password("dummy", _DUMMY_PASSWORD_HASH)
        log_auth_event(AuditEvent.LOGIN_FAILURE, email=login_data.email, ip=ip)
        return JSONResponse(status_code=400, content={"message": "Invalid email or password"})

    # Check active — use same path as unknown email to prevent enumeration
    if not user.is_active:
        verify_password("dummy", _DUMMY_PASSWORD_HASH)
        log_auth_event(AuditEvent.LOGIN_FAILURE, user_id=user.id, email=login_data.email, ip=ip)
        return JSONResponse(status_code=400, content={"message": "Invalid email or password"})

    # Verify password
    if user.password_hash is None or not verify_password(login_data.password, user.password_hash):
        locked, remaining = record_failed_login(
            db_path, login_data.email, max_attempts=max_attempts, lockout_minutes=lockout_minutes
        )
        log_auth_event(AuditEvent.LOGIN_FAILURE, user_id=user.id, email=login_data.email, ip=ip)
        if locked:
            return JSONResponse(
                status_code=423,
                content={
                    "message": "Account is temporarily locked",
                    "remaining_seconds": remaining,
                },
            )
        return JSONResponse(status_code=400, content={"message": "Invalid email or password"})

    # Success
    reset_failed_logins(db_path, login_data.email)
    raw_token, _session = create_session(
        db_path,
        user.id,
        ip_address=ip,
        user_agent=_get_user_agent(request),
        session_max_days=session_max_days,
    )
    update_user(db_path, user.id, UserUpdate(last_login=datetime.now(timezone.utc)))
    log_auth_event(AuditEvent.LOGIN_SUCCESS, user_id=user.id, email=user.email, ip=ip)

    user_response = UserResponse.model_validate(user)
    response = JSONResponse(
        content={
            "user": user_response.model_dump(mode="json"),
            "must_change_password": user.must_change_password,
        }
    )
    _set_session_cookie(response, raw_token, request)
    return response


@router.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    """Invalidate the current session and clear cookies."""
    user = _get_current_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"message": "Not authenticated"})

    db_path = _get_db_path(request)

    # Find and invalidate current session
    cookie_token = request.cookies.get(COOKIE_NAME)
    if cookie_token:
        token_hash = hash_token(cookie_token)
        session = get_session_by_token(db_path, token_hash)
        if session:
            invalidate_session(db_path, session.id)

    log_auth_event(
        AuditEvent.LOGOUT,
        user_id=user.id,
        email=user.email,
        ip=_get_client_ip(request),
    )

    response = JSONResponse(content={"success": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    response.delete_cookie("metabase.SESSION", path="/")
    return response


@router.get("/api/auth/me")
async def me(request: Request) -> JSONResponse:
    """Return the current user or auth-disabled indicator."""
    user = _get_current_user(request)
    if user is None:
        return JSONResponse(content={"auth_enabled": False})
    user_response = UserResponse.model_validate(user)
    return JSONResponse(content=user_response.model_dump(mode="json"))


@router.post("/api/auth/change-password")
async def change_password(request: Request) -> JSONResponse:
    """Change the current user's password."""
    user = _get_current_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"message": "Not authenticated"})

    db_path = _get_db_path(request)
    try:
        body = await request.json()
        data = ChangePasswordRequest(**body)
    except (ValueError, ValidationError):
        return JSONResponse(status_code=400, content={"message": "Invalid request body"})

    # Verify current password
    if user.password_hash is None or not verify_password(data.current_password, user.password_hash):
        return JSONResponse(status_code=400, content={"message": "Current password is incorrect"})

    # Check new password strength
    issues = check_password_strength(data.new_password)
    if issues:
        return JSONResponse(
            status_code=400, content={"message": "Password is too weak", "issues": issues}
        )

    # Prevent reuse of same password
    if verify_password(data.new_password, user.password_hash):
        return JSONResponse(
            status_code=400,
            content={"message": "New password must be different from current password"},
        )

    # Update password and clear must_change_password flag
    new_hash = hash_password(data.new_password)
    update_user(
        db_path,
        user.id,
        UserUpdate(password_hash=new_hash, must_change_password=False),
    )

    # Invalidate all sessions, then create a fresh one
    invalidate_all_sessions(db_path, user.id)

    auth_config = _get_auth_config(request)
    session_max_days = auth_config.session_max_days if auth_config else 30

    raw_token, _session = create_session(
        db_path,
        user.id,
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        session_max_days=session_max_days,
    )

    log_auth_event(
        AuditEvent.PASSWORD_CHANGE,
        user_id=user.id,
        email=user.email,
        ip=_get_client_ip(request),
    )

    response = JSONResponse(content={"success": True})
    _set_session_cookie(response, raw_token, request)
    return response


@router.get("/api/auth/sessions")
async def list_sessions(request: Request) -> JSONResponse:
    """List the current user's active sessions."""
    user = _get_current_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"message": "Not authenticated"})

    db_path = _get_db_path(request)
    sessions = list_user_sessions(db_path, user.id, active_only=True)
    current_hash = _get_current_token_hash(request)

    result: list[dict[str, Any]] = []
    for s in sessions:
        if s.is_partial:
            continue
        resp = SessionResponse(
            id=s.id,
            created_at=s.created_at,
            last_activity=s.last_activity,
            ip_address=s.ip_address,
            user_agent=s.user_agent,
            is_current=s.token_hash == current_hash,
        )
        result.append(resp.model_dump(mode="json"))

    return JSONResponse(content=result)


@router.delete("/api/auth/sessions/{session_id}")
async def revoke_session(session_id: str, request: Request) -> JSONResponse:
    """Revoke a specific session (not the current one)."""
    user = _get_current_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"message": "Not authenticated"})

    db_path = _get_db_path(request)

    # Ownership check
    sessions = list_user_sessions(db_path, user.id, active_only=True)
    target = None
    for s in sessions:
        if s.id == session_id:
            target = s
            break

    if target is None:
        return JSONResponse(status_code=404, content={"message": "Session not found"})

    # Cannot revoke current session — use logout instead
    current_hash = _get_current_token_hash(request)
    if target.token_hash == current_hash:
        return JSONResponse(
            status_code=400, content={"message": "Cannot revoke current session. Use logout."}
        )

    invalidate_session(db_path, session_id)
    return JSONResponse(content={"success": True})


@router.post("/api/auth/api-keys")
async def create_key(request: Request) -> JSONResponse:
    """Create a new API key. The full key is returned only once."""
    user = _get_current_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"message": "Not authenticated"})

    db_path = _get_db_path(request)
    try:
        body = await request.json()
        data = CreateApiKeyRequest(**body)
    except (ValueError, ValidationError):
        return JSONResponse(status_code=400, content={"message": "Invalid request body"})

    raw_key, api_key = create_api_key(db_path, user.id, data.name, expires_at=data.expires_at)

    log_auth_event(
        AuditEvent.API_KEY_CREATED,
        user_id=user.id,
        email=user.email,
        ip=_get_client_ip(request),
        details={"name": data.name},
    )

    resp = ApiKeyCreateResponse(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        expires_at=api_key.expires_at,
        key=raw_key,
    )
    return JSONResponse(content=resp.model_dump(mode="json"))


@router.get("/api/auth/api-keys")
async def list_keys(request: Request) -> JSONResponse:
    """List the current user's active API keys (prefix only, not full keys)."""
    user = _get_current_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"message": "Not authenticated"})

    db_path = _get_db_path(request)
    keys = list_user_api_keys(db_path, user.id, active_only=True)

    result: list[dict[str, Any]] = []
    for k in keys:
        resp = ApiKeyResponse(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
            expires_at=k.expires_at,
        )
        result.append(resp.model_dump(mode="json"))

    return JSONResponse(content=result)


@router.delete("/api/auth/api-keys/{key_id}")
async def revoke_key(key_id: str, request: Request) -> JSONResponse:
    """Revoke an API key."""
    user = _get_current_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"message": "Not authenticated"})

    db_path = _get_db_path(request)

    # Ownership check
    keys = list_user_api_keys(db_path, user.id, active_only=True)
    found = any(k.id == key_id for k in keys)
    if not found:
        return JSONResponse(status_code=404, content={"message": "API key not found"})

    revoke_api_key(db_path, key_id)

    log_auth_event(
        AuditEvent.API_KEY_REVOKED,
        user_id=user.id,
        email=user.email,
        ip=_get_client_ip(request),
        details={"key_id": key_id},
    )

    return JSONResponse(content={"success": True})


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@router.get("/login")
async def login_page(request: Request) -> HTMLResponse:
    """Render the login page."""
    return _render_template(
        "login.html",
        {
            "request": request,
            "version": dango.__version__,
            "current_page": "login",
            "subtitle": "Login",
        },
    )


@router.get("/setup")
async def setup_page(request: Request) -> HTMLResponse:
    """Render the change-password page (first-login setup)."""
    return _render_template(
        "change_password.html",
        {
            "request": request,
            "version": dango.__version__,
            "current_page": "setup",
            "subtitle": "Change Password",
        },
    )
