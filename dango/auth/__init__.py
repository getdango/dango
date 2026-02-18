"""dango/auth/__init__.py

Authentication and authorization data layer.

Exports Pydantic models for users, sessions, and API keys, plus all
CRUD functions for the auth SQLite database.
"""

from __future__ import annotations

from dango.auth.database import (
    cleanup_expired_sessions,
    create_api_key,
    create_session,
    create_user,
    deactivate_user,
    delete_user,
    get_api_key_by_hash,
    get_session_by_token,
    get_user_by_email,
    get_user_by_id,
    invalidate_all_user_sessions,
    invalidate_session,
    list_user_api_keys,
    list_user_sessions,
    list_users,
    revoke_api_key,
    update_session_activity,
    update_user,
)
from dango.auth.models import APIKey, Role, Session, User, UserCreate, UserResponse, UserUpdate

__all__ = [
    # Models
    "APIKey",
    "Role",
    "Session",
    "User",
    "UserCreate",
    "UserResponse",
    "UserUpdate",
    # User CRUD
    "create_user",
    "get_user_by_email",
    "get_user_by_id",
    "list_users",
    "update_user",
    "deactivate_user",
    "delete_user",
    # Session CRUD
    "create_session",
    "get_session_by_token",
    "update_session_activity",
    "invalidate_session",
    "invalidate_all_user_sessions",
    "list_user_sessions",
    "cleanup_expired_sessions",
    # API key CRUD
    "create_api_key",
    "get_api_key_by_hash",
    "list_user_api_keys",
    "revoke_api_key",
]
