"""
Dango Configuration Module

Handles loading, validating, and managing Dango configuration files.
"""

from .exceptions import (
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
    ProjectNotFoundError,
)
from .loader import ConfigLoader, get_config
from .models import (
    CSVSourceConfig,
    DangoConfig,
    DataSource,
    DeduplicationStrategy,
    GoogleSheetsSourceConfig,
    ProjectContext,
    ShopifySourceConfig,
    SourcesConfig,
    SourceType,
    Stakeholder,
    StripeSourceConfig,
)

__all__ = [
    # Models
    "DangoConfig",
    "ProjectContext",
    "SourcesConfig",
    "DataSource",
    "SourceType",
    "DeduplicationStrategy",
    "Stakeholder",
    "CSVSourceConfig",
    "GoogleSheetsSourceConfig",
    "StripeSourceConfig",
    "ShopifySourceConfig",
    # Loader
    "ConfigLoader",
    "get_config",
    # Exceptions
    "ConfigError",
    "ConfigNotFoundError",
    "ConfigValidationError",
    "ProjectNotFoundError",
]
