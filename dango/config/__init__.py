"""
Dango Configuration Module

Handles loading, validating, and managing Dango configuration files.
"""

from .models import (
    DangoConfig,
    ProjectContext,
    SourcesConfig,
    DataSource,
    SourceType,
    DeduplicationStrategy,
    Stakeholder,
    CSVSourceConfig,
    GoogleSheetsSourceConfig,
    StripeSourceConfig,
    ShopifySourceConfig,
)
from .loader import ConfigLoader, get_config
from .exceptions import (
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
    ProjectNotFoundError,
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
