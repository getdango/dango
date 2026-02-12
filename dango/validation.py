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

# Matches the pattern in config/models.py DataSource.validate_name_format:
# letters, numbers, underscores only.
_SOURCE_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_SOURCE_NAME_MAX_LEN = 128


def validate_source_name(name: str) -> str:
    """Validate a source name.

    Must be 1-128 characters, containing only letters, digits, and underscores
    (matching ``DataSource.validate_name_format`` in ``config/models.py``).

    Args:
        name: Source name to validate.

    Returns:
        The validated name (unchanged).

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
    return name


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
    """Strip dangerous characters from a path component.

    Removes ``/``, ``\\``, ``..``, and null bytes to prevent path-traversal
    attacks when constructing file paths from user input.

    Args:
        name: Raw path component.

    Returns:
        Sanitized string safe for use as a single path component.
    """
    # Remove null bytes
    name = name.replace("\x00", "")
    # Remove path separators
    name = name.replace("/", "").replace("\\", "")
    # Remove parent-directory traversal
    name = name.replace("..", "")
    return name
