"""dango/web/routes/users.py

Admin user management API and settings page routes.

Provides CRUD operations for user accounts (list, create, change role,
reset password, deactivate, reactivate, delete, unlock, revoke sessions)
and serves the admin user management and account settings pages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

import dango
from dango.auth.admin import get_auth_db_path
from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.database import (
    create_user,
    deactivate_user,
    delete_user,
    get_user_by_id,
    invalidate_all_user_sessions,
    list_users,
    update_user,
)
from dango.auth.models import Role, User, UserResponse, UserUpdate
from dango.auth.permissions import require_permission
from dango.auth.security import generate_invite_token, generate_temp_password, hash_password
from dango.exceptions import UserExistsError
from dango.logging import get_logger
from dango.web.models import ChangeRoleRequest, CreateUserRequest, DeleteUserConfirmation
from dango.web.routes.ui import _render_template

router = APIRouter(tags=["users"])
logger = get_logger(__name__)


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


def _count_active_admins(db_path: Path) -> int:
    """Count active admin users."""
    return sum(1 for u in list_users(db_path, active_only=True) if u.role == Role.ADMIN)


def _metabase_sync_role(db_path: Path, user_id: str, request: Request) -> None:
    """Sync role change to Metabase (best-effort)."""
    try:
        from dango.auth.metabase_sync import _load_metabase_credentials, sync_user_role

        project_root: Path = request.app.state.project_root
        creds = _load_metabase_credentials(project_root)
        if creds and creds.get("metabase_url"):
            sync_user_role(db_path, user_id, project_root, creds["metabase_url"])
    except Exception:
        logger.warning("metabase_role_sync_failed", user_id=user_id, exc_info=True)


def _metabase_deactivate(db_path: Path, user_id: str, request: Request) -> None:
    """Deactivate user in Metabase (best-effort)."""
    try:
        from dango.auth.metabase_sync import _load_metabase_credentials, deactivate_metabase_user

        project_root: Path = request.app.state.project_root
        creds = _load_metabase_credentials(project_root)
        if creds and creds.get("metabase_url"):
            deactivate_metabase_user(db_path, user_id, project_root, creds["metabase_url"])
    except Exception:
        logger.warning("metabase_deactivation_failed", user_id=user_id, exc_info=True)


def _metabase_delete(db_path: Path, user_id: str, request: Request) -> None:
    """Delete user from Metabase (best-effort)."""
    try:
        from dango.auth.metabase_sync import _load_metabase_credentials, delete_metabase_user

        project_root: Path = request.app.state.project_root
        creds = _load_metabase_credentials(project_root)
        if creds and creds.get("metabase_url"):
            delete_metabase_user(db_path, user_id, project_root, creds["metabase_url"])
    except Exception:
        logger.warning("metabase_deletion_failed", user_id=user_id, exc_info=True)


# ---------------------------------------------------------------------------
# Admin API endpoints
# ---------------------------------------------------------------------------


@router.get("/api/admin/users")
async def admin_list_users(
    request: Request,
    user: User = Depends(require_permission("users.manage")),
) -> JSONResponse:
    """List all users."""
    db_path = _get_db_path(request)
    users = list_users(db_path)
    result = [UserResponse.model_validate(u).model_dump(mode="json") for u in users]
    return JSONResponse(content=result)


@router.post("/api/admin/users")
async def admin_create_user(
    request: Request,
    user: User = Depends(require_permission("users.manage")),
) -> JSONResponse:
    """Create a new user via invite link or with a temporary password."""
    db_path = _get_db_path(request)
    try:
        body: dict[str, Any] = await request.json()
        data = CreateUserRequest(**body)
    except (ValueError, ValidationError):
        return JSONResponse(status_code=400, content={"message": "Invalid request body"})

    try:
        role = Role(data.role)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"message": f"Invalid role '{data.role}'. Must be admin, editor, or viewer."},
        )

    if data.generate_password:
        # Fallback: temp password flow
        password = generate_temp_password()
        new_user = User(
            email=data.email,
            password_hash=hash_password(password),
            role=role,
            must_change_password=True,
        )
        try:
            create_user(db_path, new_user)
        except UserExistsError:
            return JSONResponse(
                status_code=409,
                content={
                    "message": f"A user with email '{data.email.strip().lower()}' already exists"
                },
            )
        log_auth_event(
            AuditEvent.USER_CREATED,
            user_id=new_user.id,
            email=new_user.email,
            ip=_get_client_ip(request),
            details={"role": role.value, "created_by": user.email, "method": "temp_password"},
        )
        resp = UserResponse.model_validate(new_user)
        return JSONResponse(
            status_code=201,
            content={"user": resp.model_dump(mode="json"), "temp_password": password},
        )

    # Default: invite link flow
    from datetime import datetime, timedelta, timezone

    raw_token, token_hash = generate_invite_token()
    new_user = User(
        email=data.email,
        password_hash=None,
        role=role,
        invite_token_hash=token_hash,
        invite_expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
    )
    try:
        create_user(db_path, new_user)
    except UserExistsError:
        return JSONResponse(
            status_code=409,
            content={"message": f"A user with email '{data.email.strip().lower()}' already exists"},
        )
    log_auth_event(
        AuditEvent.USER_CREATED,
        user_id=new_user.id,
        email=new_user.email,
        ip=_get_client_ip(request),
        details={"role": role.value, "created_by": user.email, "method": "invite"},
    )
    resp = UserResponse.model_validate(new_user)
    base = str(request.base_url).rstrip("/")
    return JSONResponse(
        status_code=201,
        content={"user": resp.model_dump(mode="json"), "invite_url": f"{base}/invite/{raw_token}"},
    )


@router.post("/api/admin/users/{user_id}/reinvite")
async def admin_reinvite_user(
    user_id: str,
    request: Request,
    user: User = Depends(require_permission("users.manage")),
) -> JSONResponse:
    """Generate a new invite link for a user who hasn't logged in yet."""
    from datetime import datetime, timedelta, timezone

    db_path = _get_db_path(request)
    target = get_user_by_id(db_path, user_id)
    if target is None:
        return JSONResponse(status_code=404, content={"message": "User not found"})

    if not target.is_active:
        return JSONResponse(status_code=400, content={"message": "User is deactivated"})

    # Only allow reinvite for users who haven't set a password yet
    if target.password_hash is not None and target.invite_token_hash is None:
        return JSONResponse(
            status_code=400,
            content={"message": "User has already set a password. Use 'Reset Password' instead."},
        )

    raw_token, token_hash = generate_invite_token()
    update_user(
        db_path,
        user_id,
        UserUpdate(
            invite_token_hash=token_hash,
            invite_expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        ),
    )
    log_auth_event(
        AuditEvent.INVITE_RESENT,
        user_id=user_id,
        email=target.email,
        ip=_get_client_ip(request),
        details={"resent_by": user.email},
    )
    base = str(request.base_url).rstrip("/")
    return JSONResponse(content={"invite_url": f"{base}/invite/{raw_token}"})


@router.put("/api/admin/users/{user_id}/role")
async def admin_change_role(
    user_id: str,
    request: Request,
    user: User = Depends(require_permission("users.manage")),
) -> JSONResponse:
    """Change a user's role."""
    db_path = _get_db_path(request)
    try:
        body: dict[str, Any] = await request.json()
        data = ChangeRoleRequest(**body)
    except (ValueError, ValidationError):
        return JSONResponse(status_code=400, content={"message": "Invalid request body"})

    try:
        new_role = Role(data.role)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"message": f"Invalid role '{data.role}'. Must be admin, editor, or viewer."},
        )

    target = get_user_by_id(db_path, user_id)
    if target is None:
        return JSONResponse(status_code=404, content={"message": "User not found"})

    if target.role == Role.ADMIN and new_role != Role.ADMIN:
        if _count_active_admins(db_path) <= 1:
            return JSONResponse(
                status_code=409,
                content={"message": "Cannot demote the only active admin"},
            )

    old_role = target.role
    updated = update_user(db_path, user_id, UserUpdate(role=new_role))
    _metabase_sync_role(db_path, user_id, request)
    log_auth_event(
        AuditEvent.ROLE_CHANGED,
        user_id=user_id,
        email=target.email,
        ip=_get_client_ip(request),
        details={"old_role": old_role.value, "new_role": new_role.value, "changed_by": user.email},
    )
    resp = UserResponse.model_validate(updated)
    return JSONResponse(content=resp.model_dump(mode="json"))


@router.post("/api/admin/users/{user_id}/reset-password")
async def admin_reset_password(
    user_id: str,
    request: Request,
    user: User = Depends(require_permission("users.manage")),
) -> JSONResponse:
    """Reset a user's password to a temporary one."""
    db_path = _get_db_path(request)
    target = get_user_by_id(db_path, user_id)
    if target is None:
        return JSONResponse(status_code=404, content={"message": "User not found"})

    password = generate_temp_password()
    updated = update_user(
        db_path,
        user_id,
        UserUpdate(password_hash=hash_password(password), must_change_password=True),
    )
    invalidate_all_user_sessions(db_path, user_id)
    log_auth_event(
        AuditEvent.PASSWORD_RESET,
        user_id=user_id,
        email=target.email,
        ip=_get_client_ip(request),
        details={"reset_by": user.email},
    )
    resp = UserResponse.model_validate(updated)
    return JSONResponse(content={"user": resp.model_dump(mode="json"), "temp_password": password})


@router.post("/api/admin/users/{user_id}/deactivate")
async def admin_deactivate_user(
    user_id: str,
    request: Request,
    user: User = Depends(require_permission("users.manage")),
) -> JSONResponse:
    """Deactivate a user account."""
    db_path = _get_db_path(request)
    target = get_user_by_id(db_path, user_id)
    if target is None:
        return JSONResponse(status_code=404, content={"message": "User not found"})

    if user_id == user.id:
        return JSONResponse(
            status_code=409,
            content={"message": "Cannot deactivate your own account"},
        )

    if target.role == Role.ADMIN and _count_active_admins(db_path) <= 1:
        return JSONResponse(
            status_code=409,
            content={"message": "Cannot deactivate the only active admin"},
        )

    deactivate_user(db_path, user_id)
    invalidate_all_user_sessions(db_path, user_id)
    _metabase_deactivate(db_path, user_id, request)
    log_auth_event(
        AuditEvent.USER_DEACTIVATED,
        user_id=user_id,
        email=target.email,
        ip=_get_client_ip(request),
        details={"deactivated_by": user.email},
    )
    return JSONResponse(content={"success": True})


@router.post("/api/admin/users/{user_id}/reactivate")
async def admin_reactivate_user(
    user_id: str,
    request: Request,
    user: User = Depends(require_permission("users.manage")),
) -> JSONResponse:
    """Reactivate a deactivated user account."""
    db_path = _get_db_path(request)
    target = get_user_by_id(db_path, user_id)
    if target is None:
        return JSONResponse(status_code=404, content={"message": "User not found"})

    updated = update_user(db_path, user_id, UserUpdate(is_active=True))
    log_auth_event(
        AuditEvent.USER_REACTIVATED,
        user_id=user_id,
        email=target.email,
        ip=_get_client_ip(request),
        details={"reactivated_by": user.email},
    )
    resp = UserResponse.model_validate(updated)
    return JSONResponse(content=resp.model_dump(mode="json"))


@router.delete("/api/admin/users/{user_id}")
async def admin_delete_user(
    user_id: str,
    request: Request,
    user: User = Depends(require_permission("users.manage")),
) -> JSONResponse:
    """Permanently delete a user (requires email confirmation)."""
    db_path = _get_db_path(request)
    try:
        body: dict[str, Any] = await request.json()
        data = DeleteUserConfirmation(**body)
    except (ValueError, ValidationError):
        return JSONResponse(status_code=400, content={"message": "Invalid request body"})

    target = get_user_by_id(db_path, user_id)
    if target is None:
        return JSONResponse(status_code=404, content={"message": "User not found"})

    if data.confirm_email.strip().lower() != target.email:
        return JSONResponse(
            status_code=400,
            content={"message": "Confirmation email does not match user email"},
        )

    if user_id == user.id:
        return JSONResponse(
            status_code=409,
            content={"message": "Cannot delete your own account"},
        )

    if target.role == Role.ADMIN and _count_active_admins(db_path) <= 1:
        return JSONResponse(
            status_code=409,
            content={"message": "Cannot delete the only active admin"},
        )

    _metabase_delete(db_path, user_id, request)
    delete_user(db_path, user_id)
    log_auth_event(
        AuditEvent.USER_DELETED,
        user_id=user_id,
        email=target.email,
        ip=_get_client_ip(request),
        details={"deleted_by": user.email},
    )
    return JSONResponse(content={"success": True})


@router.post("/api/admin/users/{user_id}/unlock")
async def admin_unlock_user(
    user_id: str,
    request: Request,
    user: User = Depends(require_permission("users.manage")),
) -> JSONResponse:
    """Unlock a locked-out user account."""
    db_path = _get_db_path(request)
    target = get_user_by_id(db_path, user_id)
    if target is None:
        return JSONResponse(status_code=404, content={"message": "User not found"})

    update_user(db_path, user_id, UserUpdate(failed_login_attempts=0, locked_until=None))
    log_auth_event(
        AuditEvent.ACCOUNT_UNLOCKED,
        user_id=user_id,
        email=target.email,
        ip=_get_client_ip(request),
        details={"unlocked_by": user.email},
    )
    return JSONResponse(content={"success": True})


@router.post("/api/admin/users/{user_id}/revoke-sessions")
async def admin_revoke_sessions(
    user_id: str,
    request: Request,
    user: User = Depends(require_permission("users.manage")),
) -> JSONResponse:
    """Revoke all active sessions for a user."""
    db_path = _get_db_path(request)
    target = get_user_by_id(db_path, user_id)
    if target is None:
        return JSONResponse(status_code=404, content={"message": "User not found"})

    revoked_count = invalidate_all_user_sessions(db_path, user_id)
    log_auth_event(
        AuditEvent.SESSION_EXPIRED,
        user_id=user_id,
        email=target.email,
        ip=_get_client_ip(request),
        details={"action": "admin_revoke_all", "revoked_by": user.email, "count": revoked_count},
    )
    return JSONResponse(content={"success": True, "revoked_count": revoked_count})


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@router.get("/settings/users")
async def admin_users_page(
    request: Request,
    user: User = Depends(require_permission("users.manage")),
) -> HTMLResponse:
    """Render the admin user management page."""
    return _render_template(
        request,
        "admin_users.html",
        {
            "version": dango.__version__,
            "current_page": "settings",
            "subtitle": "User Management",
        },
    )


@router.get("/settings/account")
async def account_page(request: Request) -> HTMLResponse:
    """Render the user account settings page."""
    current_user = _get_current_user(request)
    if current_user is None:
        return HTMLResponse(status_code=302, headers={"Location": "/login"})

    user_json = UserResponse.model_validate(current_user).model_dump(mode="json")
    return _render_template(
        request,
        "account.html",
        {
            "version": dango.__version__,
            "current_page": "settings",
            "subtitle": "Account Settings",
            "user_json": user_json,
        },
    )
