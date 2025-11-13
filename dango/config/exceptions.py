"""
Dango Config Exceptions

Custom exceptions for configuration handling.
"""


class ConfigError(Exception):
    """Base exception for configuration errors"""
    pass


class ConfigNotFoundError(ConfigError):
    """Configuration file not found"""
    pass


class ConfigValidationError(ConfigError):
    """Configuration validation failed"""
    pass


class ProjectNotFoundError(ConfigError):
    """Not in a Dango project directory"""
    pass
