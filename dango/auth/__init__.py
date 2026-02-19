"""dango/auth/__init__.py

User authentication and access control for Dango.

Exports Pydantic models for users, sessions, and API keys, low-level
database CRUD, higher-level session/API key lifecycle functions (from
``sessions.py``), pure security utility functions (password hashing,
token generation, recovery codes), role-based permission checking,
and audit logging for security events.
"""

from __future__ import annotations

from dango.auth.admin import (
    ensure_admin,
    format_credentials_panel,
    get_auth_config_path,
    get_auth_db_path,
    is_auth_enabled,
    set_auth_enabled,
)
from dango.auth.audit import (
    AuditEvent,
    get_audit_log_path,
    log_auth_event,
    query_audit_log,
)
from dango.auth.database import (
    create_user,
    deactivate_user,
    delete_user,
    get_api_key_by_hash,
    get_session_by_token,
    get_user_by_email,
    get_user_by_id,
    list_user_api_keys,
    list_user_sessions,
    list_users,
    update_api_key_last_used,
    update_session_activity,
    update_user,
)
from dango.auth.lockout import (
    check_account_locked,
    record_failed_login,
    reset_failed_logins,
    unlock_account,
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
from dango.auth.permissions import (
    PERMISSIONS,
    ROLE_PERMISSIONS,
    check_permission,
    get_permissions,
    has_permission,
    require_permission,
)
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
from dango.auth.sessions import (
    DEFAULT_IDLE_TIMEOUT_MINUTES,
    DEFAULT_PARTIAL_SESSION_TIMEOUT_MINUTES,
    DEFAULT_SESSION_MAX_DAYS,
    cleanup_expired_sessions,
    create_api_key,
    create_session,
    invalidate_all_sessions,
    invalidate_session,
    list_api_keys,
    revoke_api_key,
    validate_api_key,
    validate_partial_session,
    validate_session,
)

__all__ = [
    # Admin utilities
    "ensure_admin",
    "format_credentials_panel",
    "get_auth_config_path",
    "get_auth_db_path",
    "is_auth_enabled",
    "set_auth_enabled",
    # Audit logging
    "AuditEvent",
    "get_audit_log_path",
    "log_auth_event",
    "query_audit_log",
    # Models
    "APIKey",
    "Role",
    "Session",
    "User",
    "UserCreate",
    "UserResponse",
    "UserUpdate",
    # Constants
    "DEFAULT_IDLE_TIMEOUT_MINUTES",
    "DEFAULT_PARTIAL_SESSION_TIMEOUT_MINUTES",
    "DEFAULT_SESSION_MAX_DAYS",
    # User CRUD (database.py)
    "create_user",
    "get_user_by_email",
    "get_user_by_id",
    "list_users",
    "update_user",
    "deactivate_user",
    "delete_user",
    # Session lifecycle (sessions.py)
    "create_session",
    "validate_session",
    "validate_partial_session",
    "invalidate_session",
    "invalidate_all_sessions",
    "cleanup_expired_sessions",
    # Low-level session access (database.py)
    "get_session_by_token",
    "update_session_activity",
    "list_user_sessions",
    # API key lifecycle (sessions.py)
    "create_api_key",
    "validate_api_key",
    "revoke_api_key",
    "list_api_keys",
    # Low-level API key access (database.py)
    "get_api_key_by_hash",
    "list_user_api_keys",
    "update_api_key_last_used",
    # Lockout functions
    "check_account_locked",
    "record_failed_login",
    "reset_failed_logins",
    "unlock_account",
    # Metabase sync
    "deactivate_metabase_user",
    "decrypt_metabase_password",
    "delete_metabase_user",
    "ensure_duckdb_readonly",
    "ensure_metabase_groups",
    "sync_all_users_to_metabase",
    "sync_user_role",
    "sync_user_to_metabase",
    # Permissions
    "PERMISSIONS",
    "ROLE_PERMISSIONS",
    "check_permission",
    "get_permissions",
    "has_permission",
    "require_permission",
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
