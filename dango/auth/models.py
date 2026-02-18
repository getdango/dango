"""dango/auth/models.py

Pydantic v2 models for the authentication and authorization system.

Defines the data shapes for users, sessions, and API keys across four tiers:
database records, creation input, partial update, and API-safe responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Role(str, Enum):
    """User roles with hierarchical permissions."""

    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class User(BaseModel):
    """Full user database record (includes sensitive fields)."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    email: str
    password_hash: str | None = None
    role: Role = Role.VIEWER
    is_active: bool = True
    totp_secret: str | None = None
    totp_enabled: bool = False
    recovery_codes: str | None = None
    oauth_provider: str | None = None
    oauth_id: str | None = None
    failed_login_attempts: int = 0
    locked_until: datetime | None = None
    metabase_user_id: int | None = None
    metabase_password_enc: str | None = None
    must_change_password: bool = False
    last_login: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        """Lowercase and strip whitespace from email."""
        return v.strip().lower()


class UserCreate(BaseModel):
    """Input validation for user creation."""

    model_config = ConfigDict(from_attributes=True)

    email: str
    password: str | None = None
    role: Role = Role.VIEWER
    oauth_provider: str | None = None
    oauth_id: str | None = None

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        """Lowercase and strip whitespace from email."""
        return v.strip().lower()


class UserUpdate(BaseModel):
    """Partial update model (all fields optional).

    Use ``model_dump(exclude_unset=True)`` to get only the fields that were
    explicitly passed. This allows setting nullable fields to ``None``
    (e.g. clearing ``locked_until``) while still omitting untouched fields.
    """

    model_config = ConfigDict(from_attributes=True)

    email: str | None = None
    password_hash: str | None = None
    role: Role | None = None
    is_active: bool | None = None
    totp_secret: str | None = None
    totp_enabled: bool | None = None
    recovery_codes: str | None = None
    oauth_provider: str | None = None
    oauth_id: str | None = None
    failed_login_attempts: int | None = None
    locked_until: datetime | None = None
    metabase_user_id: int | None = None
    metabase_password_enc: str | None = None
    must_change_password: bool | None = None
    last_login: datetime | None = None

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: str | None) -> str | None:
        """Lowercase and strip whitespace from email if provided."""
        if v is None:
            return None
        return v.strip().lower()


class UserResponse(BaseModel):
    """API-safe user representation (excludes sensitive fields)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    role: Role
    is_active: bool
    totp_enabled: bool
    oauth_provider: str | None = None
    failed_login_attempts: int = 0
    locked_until: datetime | None = None
    must_change_password: bool = False
    last_login: datetime | None = None
    created_at: datetime
    updated_at: datetime


class Session(BaseModel):
    """Full session database record."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    token_hash: str
    is_active: bool = True
    is_partial: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime
    last_activity: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ip_address: str | None = None
    user_agent: str | None = None


class APIKey(BaseModel):
    """Full API key database record."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    name: str
    key_hash: str
    key_prefix: str = ""
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
