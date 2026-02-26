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
    CloudConfig,
    CSVSourceConfig,
    DangoConfig,
    DataSource,
    DbtOverrides,
    DeduplicationStrategy,
    GoogleSheetsSourceConfig,
    ProjectContext,
    ShopifySourceConfig,
    SourcesConfig,
    SourceType,
    SpacesConfig,
    Stakeholder,
    StripeSourceConfig,
)
from .schedules import (
    CRON_PRESETS,
    ReloadResult,
    ScheduleConfig,
    SchedulesConfig,
    ScheduleType,
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
    # Cloud config models
    "CloudConfig",
    "SpacesConfig",
    "DbtOverrides",
    # Schedule config models
    "ScheduleConfig",
    "SchedulesConfig",
    "ScheduleType",
    "ReloadResult",
    "CRON_PRESETS",
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
