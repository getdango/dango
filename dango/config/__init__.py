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
from .helpers import find_project_root, get_config
from .loader import ConfigLoader
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
    # Helpers
    "find_project_root",
    "get_config",
    # Exceptions
    "ConfigError",
    "ConfigNotFoundError",
    "ConfigValidationError",
    "ProjectNotFoundError",
]
