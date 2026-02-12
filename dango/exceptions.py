"""dango/exceptions.py

Unified exception hierarchy for all Dango modules.

Every Dango exception inherits from ``DangoError`` and carries:

- ``error_code``  — stable identifier (e.g. ``DANGO-C002``) for docs / logs
- ``context``     — structured key/value dict for programmatic inspection
- ``user_message``— human-readable message (defaults to the raw message)

Subclasses define ``_default_error_code`` so that existing ``raise ConfigError("msg")``
call sites work without passing ``error_code=`` explicitly.

Debug mode
----------
Set ``DANGO_DEBUG=1`` (or ``true`` / ``yes``) to surface full stack traces in
CLI error handlers. Checked via :func:`is_debug_mode`.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = [
    "DangoError",
    "ConfigError",
    "ConfigNotFoundError",
    "ConfigValidationError",
    "ProjectNotFoundError",
    "IngestionError",
    "SyncTimeoutError",
    "CSVSchemaMismatchError",
    "InfrastructureError",
    "DiskSpaceError",
    "DuckDBHealthError",
    "DbtLockError",
    "ValidationError",
    "InvalidSourceNameError",
    "InvalidDateFormatError",
    "InvalidPortError",
    "WebAPIError",
    "is_debug_mode",
]

# ---------------------------------------------------------------------------
# Debug mode helper
# ---------------------------------------------------------------------------


def is_debug_mode() -> bool:
    """Return True when ``DANGO_DEBUG`` is enabled (``1``, ``true``, or ``yes``)."""
    return os.environ.get("DANGO_DEBUG", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Base exception
# ---------------------------------------------------------------------------


class DangoError(Exception):
    """Root of the Dango exception hierarchy.

    Args:
        message: Human-readable error description.
        error_code: Stable error identifier (e.g. ``DANGO-C002``).
            Falls back to the class-level ``_default_error_code``.
        context: Arbitrary key/value pairs for structured logging / API responses.
        user_message: Simplified message safe to display to end-users.
            Defaults to *message*.
    """

    _default_error_code: str = "DANGO-G000"

    def __init__(
        self,
        message: str = "",
        *,
        error_code: str | None = None,
        context: dict[str, Any] | None = None,
        user_message: str | None = None,
    ) -> None:
        self.error_code: str = error_code if error_code is not None else self._default_error_code
        self.context: dict[str, Any] = context if context is not None else {}
        self.user_message: str = user_message if user_message is not None else message
        super().__init__(message)

    def __repr__(self) -> str:
        cls = type(self).__name__
        return f"{cls}({str(self)!r}, error_code={self.error_code!r})"


# ---------------------------------------------------------------------------
# Config exceptions  (DANGO-C***)
# ---------------------------------------------------------------------------


class ConfigError(DangoError):
    """Base exception for configuration errors."""

    _default_error_code = "DANGO-C001"


class ConfigNotFoundError(ConfigError):
    """Configuration file not found."""

    _default_error_code = "DANGO-C002"


class ConfigValidationError(ConfigError):
    """Configuration validation failed."""

    _default_error_code = "DANGO-C003"


class ProjectNotFoundError(ConfigError):
    """Not in a Dango project directory."""

    _default_error_code = "DANGO-C004"


# ---------------------------------------------------------------------------
# Ingestion exceptions  (DANGO-I***)
# ---------------------------------------------------------------------------


class IngestionError(DangoError):
    """Base exception for data ingestion errors."""

    _default_error_code = "DANGO-I001"


class SyncTimeoutError(IngestionError):
    """Raised when a sync operation exceeds its timeout."""

    _default_error_code = "DANGO-I002"


class CSVSchemaMismatchError(IngestionError):
    """Raised when a CSV file schema doesn't match the existing table schema."""

    _default_error_code = "DANGO-I003"


# ---------------------------------------------------------------------------
# Infrastructure / Utils exceptions  (DANGO-U***)
# ---------------------------------------------------------------------------


class InfrastructureError(DangoError):
    """Base exception for infrastructure errors."""

    _default_error_code = "DANGO-U001"


class DiskSpaceError(InfrastructureError):
    """Raised when disk space is insufficient."""

    _default_error_code = "DANGO-U002"


class DuckDBHealthError(InfrastructureError):
    """Raised when a DuckDB health check fails."""

    _default_error_code = "DANGO-U003"


class DbtLockError(InfrastructureError):
    """Raised when unable to acquire the dbt lock.

    Preserves the ``lock_info`` keyword for backward compatibility with
    existing callers in ``utils/dbt_lock.py``.
    """

    _default_error_code = "DANGO-U004"

    def __init__(
        self,
        message: str = "",
        *,
        lock_info: dict[str, Any] | None = None,
        error_code: str | None = None,
        context: dict[str, Any] | None = None,
        user_message: str | None = None,
    ) -> None:
        self.lock_info = lock_info
        # Merge lock_info into context so it appears in API debug responses
        if lock_info:
            merged = dict(lock_info)
            if context:
                merged.update(context)
            context = merged
        super().__init__(
            message,
            error_code=error_code,
            context=context,
            user_message=user_message,
        )


# ---------------------------------------------------------------------------
# Validation exceptions  (DANGO-V***)
# ---------------------------------------------------------------------------


class ValidationError(DangoError):
    """Base exception for input validation errors."""

    _default_error_code = "DANGO-V001"


class InvalidSourceNameError(ValidationError):
    """Raised when a source name contains invalid characters."""

    _default_error_code = "DANGO-V002"


class InvalidDateFormatError(ValidationError):
    """Raised when a date string does not match the expected format."""

    _default_error_code = "DANGO-V003"


class InvalidPortError(ValidationError):
    """Raised when a port number is outside the valid range (1-65535)."""

    _default_error_code = "DANGO-V004"


# ---------------------------------------------------------------------------
# Web exceptions  (DANGO-W***)
# ---------------------------------------------------------------------------


class WebAPIError(DangoError):
    """Base exception for web API errors."""

    _default_error_code = "DANGO-W001"
