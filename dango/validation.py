"""dango/validation.py

Input validation and sanitization utilities for Dango.

All validators raise a specific ``ValidationError`` subclass from
``dango.exceptions`` on failure, keeping validation logic separate from
business logic.
"""

from __future__ import annotations

import re
from datetime import datetime

from dango.exceptions import (
    InvalidDateFormatError,
    InvalidPortError,
    InvalidSourceNameError,
)

__all__ = [
    "validate_source_name",
    "validate_identifier",
    "validate_date_string",
    "validate_port_range",
    "validate_limit",
    "sanitize_path_component",
]

# Stricter than config/models.py's isalnum() check: ASCII-only letters,
# digits, and underscores.  The model validator also lowercases; we do the
# same here so validated names always match what the config stores.
_SOURCE_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_SOURCE_NAME_MAX_LEN = 128


def validate_source_name(name: str) -> str:
    """Validate and normalize a source / identifier name.

    Must be 1-128 ASCII characters (letters, digits, underscores).
    Returns the **lowercased** name to match the normalization performed by
    ``DataSource.validate_name_format`` in ``config/models.py``.

    Args:
        name: Source name to validate.

    Returns:
        The validated, lowercased name.

    Raises:
        InvalidSourceNameError: If the name is empty, too long, or contains
            invalid characters.
    """
    if not name:
        raise InvalidSourceNameError(
            "Source name must not be empty.",
            context={"name": name},
        )
    if len(name) > _SOURCE_NAME_MAX_LEN:
        raise InvalidSourceNameError(
            f"Source name must be at most {_SOURCE_NAME_MAX_LEN} characters (got {len(name)}).",
            context={"name": name, "length": len(name)},
        )
    if not _SOURCE_NAME_RE.match(name):
        raise InvalidSourceNameError(
            f"Source name '{name}' is invalid. Use only letters, numbers, and underscores.",
            context={"name": name},
        )
    return name.lower()


# Alias for use with dbt model names and other identifiers that follow the
# same [a-zA-Z0-9_] pattern.
validate_identifier = validate_source_name


def validate_date_string(value: str, fmt: str = "%Y-%m-%d") -> datetime:
    """Parse and validate a date string.

    Args:
        value: Date string to validate (e.g. ``"2024-01-15"``).
        fmt: Expected ``strftime`` format (default ``%Y-%m-%d``).

    Returns:
        Parsed ``datetime`` object.

    Raises:
        InvalidDateFormatError: If the string does not match *fmt*.
    """
    try:
        return datetime.strptime(value, fmt)
    except ValueError as exc:
        raise InvalidDateFormatError(
            f"Invalid date '{value}' — expected format '{fmt}'.",
            context={"value": value, "format": fmt},
        ) from exc


def validate_port_range(port: int) -> int:
    """Validate that *port* is in the range 1-65535.

    Args:
        port: Port number to validate.

    Returns:
        The validated port number.

    Raises:
        InvalidPortError: If the port is out of range.
    """
    if not isinstance(port, int) or isinstance(port, bool):
        raise InvalidPortError(
            f"Port must be an integer, got {type(port).__name__}.",
            context={"port": port},
        )
    if port < 1 or port > 65535:
        raise InvalidPortError(
            f"Port {port} is out of range (must be 1-65535).",
            context={"port": port},
        )
    return port


def validate_limit(limit: int, max_val: int = 10000) -> int:
    """Clamp *limit* to the range ``[1, max_val]``.

    This is a graceful-degradation validator — it never raises, just clamps.

    Args:
        limit: Requested limit value.
        max_val: Upper bound (default 10 000).

    Returns:
        Clamped limit value.
    """
    if not isinstance(limit, int) or isinstance(limit, bool):
        return 1
    return max(1, min(limit, max_val))


def sanitize_path_component(name: str) -> str:
    """Sanitize a user-supplied filename for safe use as a single path component.

    Uses a two-step approach: first strips the raw name down to its basename
    (removing any directory components), then removes null bytes. The result
    is guaranteed to be a flat filename with no directory traversal.

    Args:
        name: Raw filename (e.g. from an HTTP upload).

    Returns:
        Sanitized filename.  Returns ``"unnamed"`` if the result would be
        empty after sanitization.
    """
    from pathlib import PurePosixPath

    # Remove null bytes first
    name = name.replace("\x00", "")
    # Extract just the filename (strips all directory components on both
    # POSIX and Windows-style paths)
    name = PurePosixPath(name).name
    # Also strip Windows backslash paths that PurePosixPath doesn't handle
    if "\\" in name:
        name = name.rsplit("\\", 1)[-1]
    # Reject special directory names and empty results
    if not name or name in (".", ".."):
        return "unnamed"
    return name
