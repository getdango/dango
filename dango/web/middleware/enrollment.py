"""dango/web/middleware/enrollment.py

Enrollment-only sessions: with ``auth.require_2fa`` a user who has not set up TOTP gets a partial
session from login. It reaches only the routes below (TOTP setup, logout, password change and
the account page that hosts the setup form); the full session is issued once TOTP is verified.
"""

from __future__ import annotations

from pathlib import Path

from dango.auth import sessions
from dango.auth.models import User
from dango.logging import get_logger

logger = get_logger(__name__)

ENROLLMENT_PAGE = "/settings/account?requires_2fa_setup=true"

_ENROLLMENT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/auth/2fa/setup"),
        ("POST", "/api/auth/2fa/verify-setup"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/change-password"),
        ("GET", "/api/auth/me"),
        ("GET", "/settings/account"),
    }
)


def enrollment_allows(method: str, path: str) -> bool:
    """True if an enrollment-only session may call *method* *path*."""
    return (method, path) in _ENROLLMENT_ROUTES


def enrollment_user(db_path: Path, token: str | None) -> User | None:
    """The user of a valid enrollment-only session, else ``None``.

    A partial session of a user who already has TOTP enabled is the "enter your code" state of
    login: it is not an enrollment session and reaches nothing here.
    """
    if token is None:
        return None
    try:
        user = sessions.validate_partial_session(db_path, token)
    except Exception:
        logger.debug("enrollment_session_lookup_failed", exc_info=True)
        return None
    if user is None or user.totp_enabled:
        return None
    return user
