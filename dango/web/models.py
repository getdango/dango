"""dango/web/models.py

Pydantic request/response DTOs for the web API.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

import dango


class TableInfo(BaseModel):
    """Table information for multi-resource sources."""

    name: str
    row_count: int
    schema: str  # type: ignore[assignment]  # shadows BaseModel.schema() deprecated method


class SourceStatus(BaseModel):
    """Source status information."""

    name: str
    type: str
    enabled: bool
    last_sync: str | None = None
    row_count: int | None = None
    status: str = "unknown"  # "synced", "syncing", "failed", "unknown"
    freshness: dict[str, Any] | None = None  # Data freshness information
    tables: list[TableInfo] | None = None  # Per-table breakdown for multi-resource sources
    has_schedule: bool = False
    schedule_display: str | None = None  # Human-readable schedule (e.g., "Every 6 hours")
    supports_incremental: bool = True  # Whether source supports incremental sync
    supports_date_range: bool = False  # Whether source supports custom date range sync
    sync_mode: str | None = None  # "incremental" or "full_refresh"
    lookback_days: int | None = None  # Lookback window in days (if applicable)
    write_disposition: str | None = None  # "merge" or "replace"
    needs_attention: bool = False  # Whether source has unresolved breaking drift
    attention_reason: str | None = None  # Why source needs attention
    last_sync_duration_seconds: float | None = None  # Duration of last sync


class ServiceHealth(BaseModel):
    """Service health check response."""

    status: str
    dango_version: str = dango.__version__
    services: dict[str, str]
    uptime: str


class SyncRequest(BaseModel):
    """Sync request parameters."""

    full_refresh: bool = False
    start_date: str | None = None
    end_date: str | None = None
    allow_empty_replace: bool = False


class SyncResponse(BaseModel):
    """Sync response."""

    success: bool
    message: str
    source_name: str
    started_at: str


class LogEntry(BaseModel):
    """Log entry."""

    timestamp: str
    level: str
    message: str


class WatcherStatus(BaseModel):
    """File watcher status information."""

    running: bool
    pid: int | None = None
    auto_sync_enabled: bool
    auto_dbt_enabled: bool
    debounce_seconds: int
    watch_patterns: list[str]
    watch_directories: list[str]
    log_file: str | None = None


# ---------------------------------------------------------------------------
# Auth DTOs (used by web/routes/auth.py)
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """Login credentials."""

    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)


class ChangePasswordRequest(BaseModel):
    """Password change payload."""

    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class CreateApiKeyRequest(BaseModel):
    """API key creation payload."""

    name: str = Field(max_length=128)
    expires_at: datetime | None = None


class SessionResponse(BaseModel):
    """Sanitised session for API responses."""

    id: str
    created_at: datetime
    last_activity: datetime
    ip_address: str | None
    user_agent: str | None
    is_current: bool


class ApiKeyResponse(BaseModel):
    """API key metadata (excludes the full key)."""

    id: str
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


class ApiKeyCreateResponse(ApiKeyResponse):
    """API key creation response — includes the full key shown once."""

    key: str


# ---------------------------------------------------------------------------
# Admin user management DTOs (used by web/routes/users.py)
# ---------------------------------------------------------------------------


class CreateUserRequest(BaseModel):
    """Admin user creation payload."""

    email: str = Field(max_length=254)
    role: str = Field(default="viewer", max_length=32)
    generate_password: bool = False

    @field_validator("email", mode="before")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Normalize and minimally validate the email address."""
        v = v.strip().lower()
        if not v or "@" not in v:
            raise ValueError("Invalid email address")
        return v


class AcceptInviteRequest(BaseModel):
    """Invite acceptance payload."""

    token: str = Field(max_length=1024)
    password: str = Field(max_length=1024)


class ChangeRoleRequest(BaseModel):
    """Admin role change payload."""

    role: str = Field(max_length=32)


class DeleteUserConfirmation(BaseModel):
    """Delete user confirmation payload."""

    confirm_email: str = Field(max_length=254)


# ---------------------------------------------------------------------------
# 2FA DTOs (used by web/routes/auth_2fa.py)
# ---------------------------------------------------------------------------


class TwoFASetupRequest(BaseModel):
    """Request to begin 2FA setup (verifies current password)."""

    password: str = Field(max_length=1024)


class TwoFAVerifySetupRequest(BaseModel):
    """Verify a TOTP code to complete 2FA setup."""

    code: str = Field(max_length=10)


class TwoFAVerifyRequest(BaseModel):
    """Verify a TOTP or recovery code during login."""

    code: str = Field(max_length=10)
    is_recovery: bool = False


class TwoFADisableRequest(BaseModel):
    """Request to disable 2FA (verifies current password)."""

    password: str = Field(max_length=1024)


class TwoFARegenerateRequest(BaseModel):
    """Request to regenerate recovery codes (verifies password + TOTP)."""

    password: str = Field(max_length=1024)
    code: str = Field(max_length=10)


# ---------------------------------------------------------------------------
# Schedule DTOs (used by web/routes/schedules.py)
# ---------------------------------------------------------------------------


class TriggerRequest(BaseModel):
    """Manual trigger payload (optional overrides)."""

    full_refresh: bool = False
    start_date: str | None = None
    end_date: str | None = None


# ---------------------------------------------------------------------------
# Sync trigger DTOs (used by web/routes/sync.py — remote sync trigger)
# ---------------------------------------------------------------------------


class SyncTriggerRequest(BaseModel):
    """Remote sync trigger payload."""

    sources: list[str]
    full_refresh: bool = False
    backfill: str | None = None  # "7d", "2w", "1m"
    allow_empty_replace: bool = False
