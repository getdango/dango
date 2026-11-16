"""dango/auth/password_gate.py

Current-password checks for signed-in users, counted against the login lockout.

Endpoints that re-ask for the password (change password, 2FA setup/disable/recovery codes,
API-key creation) must not be a free oracle for someone holding only a session: every wrong
answer is a failed login for the account, and a locked account is refused before the
password is compared.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.lockout import check_account_locked, record_failed_login, reset_failed_logins
from dango.auth.models import User
from dango.auth.security import verify_password
from dango.config.models import AuthConfig

LOCKED_MESSAGE = "Account is temporarily locked"


class PasswordCheck(str, Enum):
    """Outcome of a current-password check."""

    OK = "ok"
    WRONG = "wrong"
    LOCKED = "locked"


@dataclass(frozen=True)
class PasswordResult:
    """The outcome plus, when locked, the seconds left."""

    check: PasswordCheck
    remaining_seconds: int = 0


def check_current_password(
    db_path: Path,
    user: User,
    password: str | None,
    *,
    client_ip: str | None,
    auth_config: AuthConfig | None,
) -> PasswordResult:
    """Compare *password* with the user's, counting a wrong answer as a failed login."""
    max_attempts = auth_config.lockout.max_attempts if auth_config is not None else 5
    lockout_minutes = auth_config.lockout.lockout_minutes if auth_config is not None else 15

    locked, remaining = check_account_locked(db_path, user.email, client_ip=client_ip)
    if locked:
        return PasswordResult(PasswordCheck.LOCKED, remaining)

    if password is not None and user.password_hash is not None:
        if verify_password(password, user.password_hash):
            reset_failed_logins(db_path, user.email, client_ip=client_ip)
            return PasswordResult(PasswordCheck.OK)

    now_locked, remaining = record_failed_login(
        db_path,
        user.email,
        client_ip=client_ip,
        max_attempts=max_attempts,
        lockout_minutes=lockout_minutes,
    )
    log_auth_event(
        AuditEvent.LOGIN_FAILURE,
        user_id=user.id,
        email=user.email,
        ip=client_ip,
        details={"reason": "password_recheck"},
    )
    if now_locked:
        return PasswordResult(PasswordCheck.LOCKED, remaining)
    return PasswordResult(PasswordCheck.WRONG)
