"""Tests for test factories — verifies factory defaults, overrides, and composition."""

import pytest

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
from tests.factories.config_factories import (
    make_csv_source_config,
    make_dango_config,
    make_data_source,
    make_google_sheets_source_config,
    make_platform_settings,
    make_project_context,
    make_sources_config,
    make_stakeholder,
)


@pytest.mark.unit
class TestFactories:
    def test_make_stakeholder_defaults(self):
        s = make_stakeholder()
        assert isinstance(s, Stakeholder)
        assert s.name == "Test User"
        assert s.role == "Analyst"

    def test_make_stakeholder_override(self):
        s = make_stakeholder(name="Alice", role="Engineer")
        assert s.name == "Alice"
        assert s.role == "Engineer"

    def test_make_project_context_defaults(self):
        ctx = make_project_context()
        assert isinstance(ctx, ProjectContext)
        assert ctx.name == "Test Analytics"
        assert ctx.created is not None

    def test_make_data_source_csv_auto_config(self):
        ds = make_data_source(SourceType.CSV)
        assert ds.type == SourceType.CSV
        assert isinstance(ds.csv, CSVSourceConfig)

    def test_make_data_source_google_sheets_auto_config(self):
        ds = make_data_source(SourceType.GOOGLE_SHEETS)
        assert ds.type == SourceType.GOOGLE_SHEETS
        assert isinstance(ds.google_sheets, GoogleSheetsSourceConfig)

    def test_make_sources_config_default_has_one_source(self):
        sc = make_sources_config()
        assert len(sc.sources) == 1
        assert sc.sources[0].type == SourceType.CSV

    def test_make_sources_config_empty(self):
        sc = make_sources_config(sources=[])
        assert sc.sources == []

    def test_make_dango_config_composes_sub_factories(self):
        config = make_dango_config()
        assert isinstance(config, DangoConfig)
        assert isinstance(config.project, ProjectContext)
        assert isinstance(config.sources, SourcesConfig)
        assert isinstance(config.platform, PlatformSettings)
