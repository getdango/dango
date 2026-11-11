"""dango/web/routes/auth_checks.py

Route-level re-authentication checks shared by the account endpoints.

Each helper returns ``None`` when the check passes, otherwise the response to send (400 for a
wrong answer, 423 while the account is locked). Wrong passwords count as failed logins for the
account and wrong TOTP codes feed the persistent TOTP counter, so none of these endpoints is a
free guessing oracle for someone who only holds a session.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import JSONResponse

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.password_gate import LOCKED_MESSAGE, PasswordCheck, check_current_password
from dango.auth.totp_guard import (
    accept_totp_code,
    record_totp_failure,
    reset_totp_failures,
    totp_lock_remaining,
)
from dango.config.models import AuthConfig


def locked_response(remaining_seconds: int) -> JSONResponse:
    """The 423 for a locked account (same body as a locked login)."""
    return JSONResponse(
        status_code=423,
        content={"message": LOCKED_MESSAGE, "remaining_seconds": remaining_seconds},
    )


def password_failure(
    db_path: Path,
    user: User,
    password: str | None,
    *,
    client_ip: str | None,
    auth_config: AuthConfig | None,
    wrong_message: str = "Invalid password",
) -> JSONResponse | None:
    """None when *password* is the user's; otherwise the 400 (wrong) or 423 (locked) response."""
    checked = check_current_password(
        db_path, user, password, client_ip=client_ip, auth_config=auth_config
    )
    if checked.check is PasswordCheck.OK:
        return None
    if checked.check is PasswordCheck.LOCKED:
        return locked_response(checked.remaining_seconds)
    return JSONResponse(status_code=400, content={"message": wrong_message})


def totp_locked_response(seconds: int) -> JSONResponse:
    """The 423 for an account whose TOTP verification is locked."""
    return JSONResponse(
        status_code=423,
        content={
            "message": "Too many failed verification attempts. Try again later.",
            "remaining_seconds": seconds,
        },
    )


def totp_failed(db_path: Path, user: User, reason: str, *, client_ip: str | None) -> int:
    """Record a failed code (persistently and in the audit log); the lock's seconds if it locked."""
    log_auth_event(
        AuditEvent.LOGIN_FAILURE,
        user_id=user.id,
        email=user.email,
        ip=client_ip,
        details={"reason": reason},
    )
    return record_totp_failure(db_path, user.id, user.email)


def totp_failure(
    db_path: Path, user: User, code: str, *, client_ip: str | None
) -> JSONResponse | None:
    """None for a good, unused TOTP code; otherwise the 400 (bad) or 423 (locked) response."""
    locked_for = totp_lock_remaining(db_path, user.id)
    if locked_for:
        return totp_locked_response(locked_for)
    if user.totp_secret and accept_totp_code(db_path, user.id, user.email, user.totp_secret, code):
        reset_totp_failures(db_path, user.id)
        return None
    locked_for = totp_failed(db_path, user, "invalid_totp", client_ip=client_ip)
    if locked_for:
        return totp_locked_response(locked_for)
    return JSONResponse(status_code=400, content={"message": "Invalid TOTP code"})
