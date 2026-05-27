"""dango/auth/sessions.py

Session management and API key lifecycle orchestration.

Composes security utilities (token generation, hashing) with database
CRUD to provide complete session and API key workflows.  All timeout
policy enforcement lives here; ``database.py`` handles raw persistence
only.

Downstream consumers: auth middleware (TASK-015), CLI auth commands
(TASK-013), unit tests (TEST-002).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from dango.auth import database as db
from dango.auth import security
from dango.auth.models import APIKey, Session, User

# ---------------------------------------------------------------------------
# Default timeout constants (overridable per-call)
# ---------------------------------------------------------------------------

DEFAULT_IDLE_TIMEOUT_MINUTES: int = 10080
"""Idle timeout — invalidate session after this many minutes of inactivity (7 days for local)."""

DEFAULT_SESSION_MAX_DAYS: int = 365
"""Absolute timeout — session expires this many days after creation (1 year for local)."""

DEFAULT_PARTIAL_SESSION_TIMEOUT_MINUTES: int = 5
"""Partial session timeout — for 2FA intermediate state."""


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def create_session(
    db_path: Path,
    user_id: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
    session_max_days: int = DEFAULT_SESSION_MAX_DAYS,
    is_partial: bool = False,
    partial_timeout_minutes: int = DEFAULT_PARTIAL_SESSION_TIMEOUT_MINUTES,
) -> tuple[str, Session]:
    """Create a new session and return ``(raw_token, session)``.

    The raw token is shown to the caller once (set as cookie);
    only the SHA-256 hash is persisted in the database.

    Args:
        db_path: Path to the auth SQLite database.
        user_id: ID of the user this session belongs to.
        ip_address: Optional client IP address.
        user_agent: Optional client User-Agent string.
        session_max_days: Absolute session lifetime in days.
        is_partial: If ``True``, creates a short-lived partial session
            for 2FA intermediate state.
        partial_timeout_minutes: Lifetime for partial sessions in minutes.

    Returns:
        Tuple of ``(raw_token, Session)``.
    """
    now = datetime.now(timezone.utc)
    raw_token = security.generate_session_token()
    token_hash = security.hash_token(raw_token)

    if is_partial:
        expires_at = now + timedelta(minutes=partial_timeout_minutes)
    else:
        expires_at = now + timedelta(days=session_max_days)

    session = Session(
        user_id=user_id,
        token_hash=token_hash,
        is_active=True,
        is_partial=is_partial,
        created_at=now,
        expires_at=expires_at,
        last_activity=now,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.create_session(db_path, session)
    return raw_token, session


def validate_session(
    db_path: Path,
    token: str,
    *,
    idle_timeout_minutes: int = DEFAULT_IDLE_TIMEOUT_MINUTES,
) -> User | None:
    """Validate a session token and return the associated user.

    Enforces absolute expiry, idle timeout, active status, non-partial
    requirement, and user active status.  On success, updates the
    session's sliding-window ``last_activity`` timestamp.

    Args:
        db_path: Path to the auth SQLite database.
        token: Raw session token (from cookie).
        idle_timeout_minutes: Maximum minutes of inactivity before
            the session is considered expired.

    Returns:
        The ``User`` if the session is valid, or ``None``.
    """
    now = datetime.now(timezone.utc)
    token_hash = security.hash_token(token)

    session = db.get_session_by_token(db_path, token_hash)
    if session is None:
        return None
    if not session.is_active:
        return None
    if session.is_partial:
        return None

    # Absolute timeout
    if now >= session.expires_at:
        db.invalidate_session(db_path, session.id)
        return None

    # Idle timeout
    idle_delta = timedelta(minutes=idle_timeout_minutes)
    if now - session.last_activity > idle_delta:
        db.invalidate_session(db_path, session.id)
        return None

    # Check user is active (before updating activity timestamp —
    # don't record "last activity" for failed auth attempts)
    user = db.get_user_by_id(db_path, session.user_id)
    if user is None or not user.is_active:
        return None

    # Sliding window — update last_activity
    db.update_session_activity(db_path, session.id)
    return user


def validate_partial_session(
    db_path: Path,
    token: str,
) -> User | None:
    """Validate a partial session token (2FA intermediate state).

    Unlike ``validate_session``, this *requires* ``is_partial=True``
    and does **not** apply idle timeout or update ``last_activity``.
    Only absolute expiry and active-status checks apply.

    Args:
        db_path: Path to the auth SQLite database.
        token: Raw session token.

    Returns:
        The ``User`` if the partial session is valid, or ``None``.
    """
    now = datetime.now(timezone.utc)
    token_hash = security.hash_token(token)

    session = db.get_session_by_token(db_path, token_hash)
    if session is None:
        return None
    if not session.is_active:
        return None
    if not session.is_partial:
        return None

    # Absolute timeout only
    if now >= session.expires_at:
        db.invalidate_session(db_path, session.id)
        return None

    # Check user is active
    user = db.get_user_by_id(db_path, session.user_id)
    if user is None or not user.is_active:
        return None

    return user


def invalidate_session(db_path: Path, session_id: str) -> None:
    """Mark a single session as inactive."""
    db.invalidate_session(db_path, session_id)


def invalidate_all_sessions(db_path: Path, user_id: str) -> int:
    """Invalidate all active sessions for a user. Returns count."""
    return db.invalidate_all_user_sessions(db_path, user_id)


def cleanup_expired_sessions(db_path: Path) -> int:
    """Delete expired sessions. Returns number of rows deleted."""
    return db.cleanup_expired_sessions(db_path)


# ---------------------------------------------------------------------------
# API key lifecycle
# ---------------------------------------------------------------------------


def create_api_key(
    db_path: Path,
    user_id: str,
    name: str,
    *,
    expires_at: datetime | None = None,
) -> tuple[str, APIKey]:
    """Create a new API key and return ``(raw_key, api_key)``.

    The raw key (prefixed ``dango_ak_``) is shown to the user once;
    only the SHA-256 hash is stored in the database.

    Args:
        db_path: Path to the auth SQLite database.
        user_id: ID of the user this key belongs to.
        name: Human-readable name for the key.
        expires_at: Optional expiry datetime (``None`` = never expires).

    Returns:
        Tuple of ``(raw_key, APIKey)``.
    """
    raw_key, key_hash = security.generate_api_key()
    key_prefix = security.get_key_prefix(raw_key)

    api_key = APIKey(
        user_id=user_id,
        name=name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        is_active=True,
        expires_at=expires_at,
    )
    db.create_api_key(db_path, api_key)
    return raw_key, api_key


def validate_api_key(db_path: Path, key: str) -> User | None:
    """Validate an API key and return the associated user.

    Checks active status, expiry, and user active status.  On success,
    updates the key's ``last_used_at`` timestamp.

    Args:
        db_path: Path to the auth SQLite database.
        key: Full API key string (including ``dango_ak_`` prefix).

    Returns:
        The ``User`` if the key is valid, or ``None``.
    """
    now = datetime.now(timezone.utc)
    key_hash = security.hash_api_key(key)

    api_key = db.get_api_key_by_hash(db_path, key_hash)
    if api_key is None:
        return None
    if not api_key.is_active:
        return None

    # Expiry check (None = never expires)
    if api_key.expires_at is not None and now >= api_key.expires_at:
        return None

    # Check user is active (before updating last_used —
    # don't record usage for failed auth attempts)
    user = db.get_user_by_id(db_path, api_key.user_id)
    if user is None or not user.is_active:
        return None

    # Update last_used_at
    db.update_api_key_last_used(db_path, api_key.id)
    return user


def revoke_api_key(db_path: Path, key_id: str) -> None:
    """Revoke an API key (mark as inactive)."""
    db.revoke_api_key(db_path, key_id)


def list_api_keys(db_path: Path, user_id: str) -> list[APIKey]:
    """List active API keys for a user."""
    return db.list_user_api_keys(db_path, user_id, active_only=True)
