"""tests/factories/config_factories.py

Each function returns a valid model instance with sensible defaults. All fields are overridable via keyword arguments.
"""

from dango.config.models import (
    CSVSourceConfig,
    DangoConfig,
    DataSource,
    GoogleSheetsSourceConfig,
    PlatformSettings,
    ProjectContext,
    SourcesConfig,
    SourceType,
    Stakeholder,
)


def make_stakeholder(**overrides) -> Stakeholder:
    """Create a valid Stakeholder instance."""
    defaults = {
        "name": "Test User",
        "role": "Analyst",
        "contact": "analyst@example.com",
    }
    return Stakeholder(**{**defaults, **overrides})


def make_project_context(**overrides) -> ProjectContext:
    """Create a valid ProjectContext instance."""
    defaults = {
        "name": "Test Analytics",
        "created_by": "test@example.com",
        "purpose": "Unit testing",
    }
    return ProjectContext(**{**defaults, **overrides})


def make_csv_source_config(**overrides) -> CSVSourceConfig:
    """Create a valid CSVSourceConfig instance."""
    defaults = {
        "directory": "data/test_csv",
    }
    return CSVSourceConfig(**{**defaults, **overrides})


def make_google_sheets_source_config(**overrides) -> GoogleSheetsSourceConfig:
    """Create a valid GoogleSheetsSourceConfig instance."""
    defaults = {
        "spreadsheet_url_or_id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
        "range_names": ["Sheet1"],
    }
    return GoogleSheetsSourceConfig(**{**defaults, **overrides})


def make_data_source(source_type: SourceType = SourceType.CSV, **overrides) -> DataSource:
    """Create a valid DataSource instance with type-specific config.

    Automatically populates the type-specific config field for CSV and
    Google Sheets sources. Other types get no extra config by default.
    """
    defaults = {
        "name": "test_source",
        "type": source_type,
    }
    if source_type == SourceType.CSV and "csv" not in overrides:
        defaults["csv"] = make_csv_source_config()
    elif source_type == SourceType.GOOGLE_SHEETS and "google_sheets" not in overrides:
        defaults["google_sheets"] = make_google_sheets_source_config()

    return DataSource(**{**defaults, **overrides})


def make_sources_config(sources=None, **overrides) -> SourcesConfig:
    """Create a valid SourcesConfig instance.

    Args:
        sources: List of DataSource instances. Defaults to one CSV source.
                 Pass an empty list for no sources.
    """
    if sources is None:
        sources = [make_data_source()]
    defaults = {
        "sources": sources,
    }
    return SourcesConfig(**{**defaults, **overrides})


def make_platform_settings(**overrides) -> PlatformSettings:
    """Create a PlatformSettings instance with Pydantic defaults."""
    return PlatformSettings(**overrides)


def make_dango_config(
    project: ProjectContext | None = None,
    sources: SourcesConfig | None = None,
    platform: PlatformSettings | None = None,
    **overrides,
) -> DangoConfig:
    """Create a valid DangoConfig instance.

    Composes sub-factories for any component not explicitly provided.
    """
    defaults = {
        "project": project or make_project_context(),
        "sources": sources or make_sources_config(),
        "platform": platform or make_platform_settings(),
    }
    return DangoConfig(**{**defaults, **overrides})
