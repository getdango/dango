"""dango/auth/__init__.py

User authentication and access control for Dango.

Exports Pydantic models for users, sessions, and API keys, CRUD
functions for the auth SQLite database, and pure security utility
functions (password hashing, token generation, recovery codes).
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
from dango.auth.metabase_sync import (
    deactivate_metabase_user,
    decrypt_metabase_password,
    delete_metabase_user,
    ensure_duckdb_readonly,
    ensure_metabase_groups,
    sync_all_users_to_metabase,
    sync_user_role,
    sync_user_to_metabase,
)
from dango.auth.models import APIKey, Role, Session, User, UserCreate, UserResponse, UserUpdate
from dango.auth.security import (
    check_password_strength,
    generate_api_key,
    generate_recovery_codes,
    generate_session_token,
    generate_temp_password,
    get_key_prefix,
    hash_api_key,
    hash_password,
    hash_recovery_code,
    hash_token,
    verify_password,
)

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
    # Metabase sync
    "deactivate_metabase_user",
    "decrypt_metabase_password",
    "delete_metabase_user",
    "ensure_duckdb_readonly",
    "ensure_metabase_groups",
    "sync_all_users_to_metabase",
    "sync_user_role",
    "sync_user_to_metabase",
    # Security utilities
    "check_password_strength",
    "generate_api_key",
    "generate_recovery_codes",
    "generate_session_token",
    "generate_temp_password",
    "get_key_prefix",
    "hash_api_key",
    "hash_password",
    "hash_recovery_code",
    "hash_token",
    "verify_password",
]
