"""dango/config/exceptions.py

Re-export shim — all exception classes now live in ``dango.exceptions``.

Existing imports like ``from dango.config.exceptions import ConfigError``
continue to work unchanged.
"""

from dango.exceptions import (  # noqa: F401
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
    ProjectNotFoundError,
)

__all__ = [
    "ConfigError",
    "ConfigNotFoundError",
    "ConfigValidationError",
    "ProjectNotFoundError",
]
