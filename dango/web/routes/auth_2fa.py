"""dango/web/routes/auth_2fa.py

API endpoints for TOTP two-factor authentication.

Provides setup, verification, disable, and recovery code regeneration
endpoints.  The ``/api/auth/2fa/verify`` endpoint is public (uses a
partial session cookie) while all others require a full authenticated
session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from dango.auth.admin import get_auth_db_path
from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.database import get_session_by_token, get_user_by_id, update_user
from dango.auth.models import User, UserResponse, UserUpdate
from dango.auth.security import generate_recovery_codes, hash_token, verify_password
from dango.auth.sessions import (
    DEFAULT_SESSION_MAX_DAYS,
    create_session,
    invalidate_session,
    validate_partial_session,
)
from dango.auth.totp import (
    consume_recovery_code,
    disable_totp,
    enable_totp,
    generate_totp_secret,
    get_provisioning_uri,
    regenerate_recovery_codes,
    setup_totp,
    verify_totp_code,
)
from dango.config.models import AuthConfig
from dango.logging import get_logger
from dango.web.middleware.auth import COOKIE_NAME, is_secure_request
from dango.web.models import (
    TwoFADisableRequest,
    TwoFARegenerateRequest,
    TwoFASetupRequest,
    TwoFAVerifyRequest,
    TwoFAVerifySetupRequest,
)
from dango.web.routes.auth import _bridge_metabase_session

router = APIRouter(tags=["auth-2fa"])
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 2FA brute-force protection
#
# Track failed verification attempts per partial session token.  After
# _MAX_2FA_ATTEMPTS failures the partial session is invalidated, forcing
# the user to re-authenticate with credentials.
#
# In-memory is acceptable: partial sessions live ≤5 min and server restart
# clears both counters and sessions.  Multi-worker: each worker tracks
# independently — worst case 5×N attempts across N workers, still finite.
# ---------------------------------------------------------------------------

_MAX_2FA_ATTEMPTS = 5
_2fa_attempt_counts: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Internal helpers (same pattern as auth.py — small, redefined here to
# avoid cross-importing private functions between route modules)
# ---------------------------------------------------------------------------


def _get_db_path(request: Request) -> Path:
    """Resolve the auth database path from application state."""
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


def _set_session_cookie(
    response: JSONResponse,
    token: str,
    request: Request,
    *,
    max_age_seconds: int,
) -> None:
    """Set the session cookie on a response with appropriate security flags."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_secure_request(request.scope),
        max_age=max_age_seconds,
    )


def _get_auth_config(request: Request) -> AuthConfig | None:
    """Load AuthConfig from project config. Returns None on failure."""
    try:
        from dango.config.helpers import load_config

        project_root: Path = request.app.state.project_root
        config = load_config(project_root)
        return config.auth
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api/auth/2fa/setup")
async def setup_2fa(request: Request) -> JSONResponse:
    """Begin 2FA setup: verify password, generate secret and recovery codes."""
    user = _get_current_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"message": "Not authenticated"})

    db_path = _get_db_path(request)
    try:
        body = await request.json()
        data = TwoFASetupRequest(**body)
    except (ValueError, ValidationError):
        return JSONResponse(status_code=400, content={"message": "Invalid request body"})

    # Verify password before allowing setup
    if user.password_hash is None or not verify_password(data.password, user.password_hash):
        return JSONResponse(status_code=400, content={"message": "Invalid password"})

    # Reject if 2FA is already active — user must disable first
    current_user = get_user_by_id(db_path, user.id)
    if current_user is not None and current_user.totp_enabled:
        return JSONResponse(
            status_code=400,
            content={"message": "2FA is already enabled. Disable it first to re-setup."},
        )

    # Generate secret and recovery codes
    secret = generate_totp_secret()
    codes = generate_recovery_codes()

    # Store (totp_enabled stays False until verify-setup)
    setup_totp(db_path, user.id, secret, codes)

    uri = get_provisioning_uri(secret, user.email)
    return JSONResponse(
        content={
            "provisioning_uri": uri,
            "secret": secret,
            "recovery_codes": codes,
        }
    )


@router.post("/api/auth/2fa/verify-setup")
async def verify_setup_2fa(request: Request) -> JSONResponse:
    """Complete 2FA setup by verifying a TOTP code."""
    user = _get_current_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"message": "Not authenticated"})

    db_path = _get_db_path(request)
    try:
        body = await request.json()
        data = TwoFAVerifySetupRequest(**body)
    except (ValueError, ValidationError):
        return JSONResponse(status_code=400, content={"message": "Invalid request body"})

    # Re-read user to get the pending totp_secret
    current_user = get_user_by_id(db_path, user.id)
    if current_user is None:
        return JSONResponse(status_code=401, content={"message": "User not found"})

    if current_user.totp_enabled:
        return JSONResponse(status_code=400, content={"message": "2FA is already enabled"})

    if not current_user.totp_secret:
        return JSONResponse(
            status_code=400,
            content={"message": "No pending 2FA setup. Call /api/auth/2fa/setup first."},
        )

    # Verify the code against the pending secret
    if not verify_totp_code(current_user.totp_secret, data.code):
        return JSONResponse(status_code=400, content={"message": "Invalid verification code"})

    enable_totp(db_path, user.id)
    return JSONResponse(content={"success": True})


@router.post("/api/auth/2fa/verify")
async def verify_2fa(request: Request) -> JSONResponse:
    """Verify a TOTP or recovery code to upgrade a partial session to full.

    This endpoint is public (bypasses auth middleware). It reads the
    partial session cookie directly and validates it.
    """
    db_path = _get_db_path(request)
    try:
        body = await request.json()
        data = TwoFAVerifyRequest(**body)
    except (ValueError, ValidationError):
        return JSONResponse(status_code=400, content={"message": "Invalid request body"})

    # Extract partial session from cookie
    cookie_token = request.cookies.get(COOKIE_NAME)
    if not cookie_token:
        return JSONResponse(status_code=401, content={"message": "Invalid or expired session"})

    # Check brute-force counter before validating session
    if _2fa_attempt_counts.get(cookie_token, 0) >= _MAX_2FA_ATTEMPTS:
        # Invalidate partial session to force re-authentication
        token_hash = hash_token(cookie_token)
        partial = get_session_by_token(db_path, token_hash)
        if partial:
            invalidate_session(db_path, partial.id)
        _2fa_attempt_counts.pop(cookie_token, None)
        return JSONResponse(
            status_code=401,
            content={"message": "Too many failed attempts. Please log in again."},
        )

    user = validate_partial_session(db_path, cookie_token)
    if user is None:
        return JSONResponse(status_code=401, content={"message": "Invalid or expired session"})

    # Re-read user for current TOTP state
    current_user = get_user_by_id(db_path, user.id)
    if current_user is None or not current_user.totp_enabled or not current_user.totp_secret:
        return JSONResponse(
            status_code=400, content={"message": "2FA is not enabled for this account"}
        )

    # Verify code (TOTP or recovery)
    verified = False
    if data.is_recovery:
        verified = consume_recovery_code(
            db_path, current_user.id, current_user.recovery_codes, data.code
        )
    else:
        verified = verify_totp_code(current_user.totp_secret, data.code)

    if not verified:
        _2fa_attempt_counts[cookie_token] = _2fa_attempt_counts.get(cookie_token, 0) + 1
        remaining = _MAX_2FA_ATTEMPTS - _2fa_attempt_counts[cookie_token]
        if remaining <= 0:
            # Invalidate partial session immediately
            token_hash = hash_token(cookie_token)
            partial = get_session_by_token(db_path, token_hash)
            if partial:
                invalidate_session(db_path, partial.id)
            _2fa_attempt_counts.pop(cookie_token, None)
            return JSONResponse(
                status_code=401,
                content={"message": "Too many failed attempts. Please log in again."},
            )
        return JSONResponse(status_code=400, content={"message": "Invalid verification code"})

    # Success — clean up brute-force counter
    _2fa_attempt_counts.pop(cookie_token, None)

    # Invalidate the partial session
    token_hash = hash_token(cookie_token)
    partial_session = get_session_by_token(db_path, token_hash)
    if partial_session:
        invalidate_session(db_path, partial_session.id)

    # Create full session
    auth_config = _get_auth_config(request)
    session_max_days = auth_config.session_max_days if auth_config else DEFAULT_SESSION_MAX_DAYS

    raw_token, _session = create_session(
        db_path,
        current_user.id,
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        session_max_days=session_max_days,
    )

    update_user(db_path, current_user.id, UserUpdate(last_login=datetime.now(timezone.utc)))
    log_auth_event(
        AuditEvent.LOGIN_SUCCESS,
        user_id=current_user.id,
        email=current_user.email,
        ip=_get_client_ip(request),
    )

    user_response = UserResponse.model_validate(current_user)
    response = JSONResponse(
        content={
            "user": user_response.model_dump(mode="json"),
            "must_change_password": current_user.must_change_password,
        }
    )
    _set_session_cookie(response, raw_token, request, max_age_seconds=session_max_days * 86400)

    await _bridge_metabase_session(current_user, request, response, log_context="2fa_verify")

    return response


@router.post("/api/auth/2fa/disable")
async def disable_2fa(request: Request) -> JSONResponse:
    """Disable 2FA for the current user (requires password verification)."""
    user = _get_current_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"message": "Not authenticated"})

    db_path = _get_db_path(request)
    try:
        body = await request.json()
        data = TwoFADisableRequest(**body)
    except (ValueError, ValidationError):
        return JSONResponse(status_code=400, content={"message": "Invalid request body"})

    # Verify password
    if user.password_hash is None or not verify_password(data.password, user.password_hash):
        return JSONResponse(status_code=400, content={"message": "Invalid password"})

    # Check that 2FA is actually enabled
    current_user = get_user_by_id(db_path, user.id)
    if current_user is None or not current_user.totp_enabled:
        return JSONResponse(status_code=400, content={"message": "2FA is not enabled"})

    disable_totp(db_path, user.id)
    return JSONResponse(content={"success": True})


@router.post("/api/auth/2fa/regenerate-recovery")
async def regenerate_recovery(request: Request) -> JSONResponse:
    """Regenerate recovery codes (requires password + TOTP verification)."""
    user = _get_current_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"message": "Not authenticated"})

    db_path = _get_db_path(request)
    try:
        body = await request.json()
        data = TwoFARegenerateRequest(**body)
    except (ValueError, ValidationError):
        return JSONResponse(status_code=400, content={"message": "Invalid request body"})

    # Verify password
    if user.password_hash is None or not verify_password(data.password, user.password_hash):
        return JSONResponse(status_code=400, content={"message": "Invalid password"})

    # Verify TOTP code
    current_user = get_user_by_id(db_path, user.id)
    if current_user is None or not current_user.totp_enabled or not current_user.totp_secret:
        return JSONResponse(status_code=400, content={"message": "2FA is not enabled"})

    if not verify_totp_code(current_user.totp_secret, data.code):
        return JSONResponse(status_code=400, content={"message": "Invalid TOTP code"})

    codes = regenerate_recovery_codes(db_path, user.id)
    return JSONResponse(content={"recovery_codes": codes})
