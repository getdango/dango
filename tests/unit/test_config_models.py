"""tests/unit/test_config_models.py

Tests for dango.config.models — Pydantic models and enums.
"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from dango.config.models import (
    CSVSourceConfig,
    DangoConfig,
    DataSource,
    DeduplicationStrategy,
    DltNativeConfig,
    FacebookAdsSourceConfig,
    GitHubSourceConfig,
    GoogleAnalyticsSourceConfig,
    GoogleSheetsSourceConfig,
    HubSpotSourceConfig,
    LocalFilesSourceConfig,
    PlatformSettings,
    ProjectContext,
    RESTAPISourceConfig,
    SalesforceSourceConfig,
    SlackSourceConfig,
    SourceType,
    Stakeholder,
    StripeSourceConfig,
)
from tests.factories.config_factories import (
    make_csv_source_config,
    make_dango_config,
    make_data_source,
    make_google_sheets_source_config,
    make_project_context,
    make_sources_config,
    make_stakeholder,
)


@pytest.mark.unit
class TestSourceType:
    def test_has_expected_members(self) -> None:
        expected = {"csv", "local_files", "google_sheets", "hubspot", "stripe", "shopify", "slack"}
        actual = {member.value for member in SourceType}
        assert expected.issubset(actual)

    def test_values_are_lowercase_strings(self) -> None:
        for member in SourceType:
            assert member.value == member.value.lower()
            assert isinstance(member.value, str)

    def test_member_count(self) -> None:
        assert len(SourceType) == 35


@pytest.mark.unit
class TestDeduplicationStrategy:
    def test_has_four_members(self) -> None:
        assert len(DeduplicationStrategy) == 4

    def test_expected_values(self) -> None:
        expected = {"none", "latest_only", "append_only", "scd_type2"}
        actual = {m.value for m in DeduplicationStrategy}
        assert actual == expected


@pytest.mark.unit
class TestStakeholder:
    def test_valid_creation(self) -> None:
        s = make_stakeholder()
        assert s.name == "Test User"
        assert s.role == "Analyst"
        assert s.contact == "analyst@example.com"

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            Stakeholder(name="Alice", role="Analyst")  # missing contact


@pytest.mark.unit
class TestProjectContext:
    def test_project_context_creation(self, sample_project_context) -> None:
        ctx = sample_project_context
        assert ctx.name == "Test Analytics"
        assert ctx.created_by == "test@example.com"
        assert ctx.purpose == "Unit testing"

    def test_minimal_required_fields(self) -> None:
        ctx = ProjectContext(name="P", created_by="a@b.com", purpose="test")
        assert ctx.name == "P"

    def test_auto_generated_created_datetime(self) -> None:
        ctx = make_project_context()
        assert isinstance(ctx.created, datetime)

    def test_all_optional_fields(self) -> None:
        ctx = make_project_context(
            organization="Acme",
            dango_version="1.0.0",
            sla="Daily by 9am",
            limitations="None known",
            getting_started="Run dango start",
            stakeholders=[make_stakeholder()],
        )
        assert ctx.organization == "Acme"
        assert ctx.dango_version == "1.0.0"
        assert ctx.sla == "Daily by 9am"
        assert len(ctx.stakeholders) == 1

    def test_missing_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            ProjectContext(created_by="a@b.com", purpose="test")

    def test_missing_created_by_raises(self) -> None:
        with pytest.raises(ValidationError):
            ProjectContext(name="P", purpose="test")

    def test_missing_purpose_raises(self) -> None:
        with pytest.raises(ValidationError):
            ProjectContext(name="P", created_by="a@b.com")

    def test_stakeholders_list(self) -> None:
        s1 = make_stakeholder(name="Alice")
        s2 = make_stakeholder(name="Bob")
        ctx = make_project_context(stakeholders=[s1, s2])
        assert len(ctx.stakeholders) == 2
        assert ctx.stakeholders[0].name == "Alice"


@pytest.mark.unit
class TestCSVSourceConfig:
    def test_defaults(self) -> None:
        cfg = make_csv_source_config()
        assert cfg.file_pattern == "*.csv"
        assert cfg.deduplication_strategy == DeduplicationStrategy.LATEST_ONLY
        assert cfg.primary_key is None
        assert cfg.timestamp_column is None

    def test_custom_pattern(self) -> None:
        cfg = make_csv_source_config(file_pattern="*.tsv")
        assert cfg.file_pattern == "*.tsv"

    def test_all_dedup_strategies(self) -> None:
        for strategy in DeduplicationStrategy:
            cfg = make_csv_source_config(deduplication_strategy=strategy)
            assert cfg.deduplication_strategy == strategy

    def test_primary_key_and_timestamp(self) -> None:
        cfg = make_csv_source_config(
            primary_key="id",
            timestamp_column="updated_at",
        )
        assert cfg.primary_key == "id"
        assert cfg.timestamp_column == "updated_at"


@pytest.mark.unit
class TestLocalFilesSourceConfig:
    def test_inherits_from_csv(self) -> None:
        cfg = LocalFilesSourceConfig(directory="data/uploads")
        assert isinstance(cfg, CSVSourceConfig)

    def test_default_file_pattern_is_star(self) -> None:
        cfg = LocalFilesSourceConfig(directory="data/uploads")
        assert cfg.file_pattern == "*"

    def test_inherits_dedup_defaults(self) -> None:
        cfg = LocalFilesSourceConfig(directory="data/uploads")
        assert cfg.deduplication_strategy == DeduplicationStrategy.LATEST_ONLY
        assert cfg.primary_key is None
        assert cfg.timestamp_column is None

    def test_custom_file_pattern(self) -> None:
        cfg = LocalFilesSourceConfig(directory="data/uploads", file_pattern="*.json")
        assert cfg.file_pattern == "*.json"

    def test_all_csv_fields_available(self) -> None:
        cfg = LocalFilesSourceConfig(
            directory="data/uploads",
            file_pattern="*.parquet",
            deduplication_strategy=DeduplicationStrategy.SCD_TYPE2,
            primary_key="id",
            timestamp_column="updated_at",
            notes="Test notes",
        )
        assert cfg.primary_key == "id"
        assert cfg.timestamp_column == "updated_at"
        assert cfg.notes == "Test notes"

    def test_data_source_local_files_field(self) -> None:
        ds = DataSource(
            name="my_files",
            type=SourceType.LOCAL_FILES,
            local_files=LocalFilesSourceConfig(directory="data/uploads"),
        )
        assert ds.local_files is not None
        assert ds.local_files.file_pattern == "*"


@pytest.mark.unit
class TestGoogleSheetsSourceConfig:
    def test_valid_creation(self) -> None:
        cfg = make_google_sheets_source_config()
        assert cfg.spreadsheet_url_or_id.startswith("1Bxi")
        assert cfg.range_names == ["Sheet1"]

    def test_ensure_list_validator_single_string(self) -> None:
        cfg = GoogleSheetsSourceConfig(
            spreadsheet_url_or_id="abc123",
            range_names="SingleSheet",
        )
        assert cfg.range_names == ["SingleSheet"]

    def test_multiple_ranges(self) -> None:
        cfg = make_google_sheets_source_config(range_names=["Sheet1", "Sheet2"])
        assert len(cfg.range_names) == 2

    def test_missing_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            GoogleSheetsSourceConfig()


@pytest.mark.unit
class TestSourceConfigModels:
    """Parametrized smoke test covering creation of all source config models."""

    @pytest.mark.parametrize(
        "model_cls,kwargs",
        [
            (StripeSourceConfig, {}),
            (FacebookAdsSourceConfig, {"account_id": "act_123456789"}),
            (GoogleAnalyticsSourceConfig, {"property_id": "123456"}),
            (HubSpotSourceConfig, {}),
            (SalesforceSourceConfig, {}),
            (GitHubSourceConfig, {"owner": "getdango", "name": "dango"}),
            (SlackSourceConfig, {}),
            (
                RESTAPISourceConfig,
                {
                    "base_url": "https://api.example.com",
                    "endpoints": [{"path": "/users"}],
                },
            ),
            (
                DltNativeConfig,
                {
                    "source_module": "my_source",
                    "source_function": "my_func",
                },
            ),
        ],
        ids=[
            "Stripe",
            "FacebookAds",
            "GoogleAnalytics",
            "HubSpot",
            "Salesforce",
            "GitHub",
            "Slack",
            "RESTAPI",
            "DltNative",
        ],
    )
    def test_source_config_creation(self, model_cls, kwargs) -> None:
        instance = model_cls(**kwargs)
        assert instance is not None


@pytest.mark.unit
class TestDataSource:
    def test_valid_underscore_name(self) -> None:
        ds = make_data_source(name="my_source")
        assert ds.name == "my_source"

    def test_valid_numeric_name(self) -> None:
        ds = make_data_source(name="source123")
        assert ds.name == "source123"

    def test_name_lowercased(self) -> None:
        ds = make_data_source(name="MySource")
        assert ds.name == "mysource"

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValidationError, match="invalid"):
            DataSource(name="", type=SourceType.CSV)

    def test_rejects_hyphenated_name(self) -> None:
        with pytest.raises(ValidationError, match="invalid"):
            DataSource(name="my-source", type=SourceType.CSV)

    def test_rejects_spaces(self) -> None:
        with pytest.raises(ValidationError, match="invalid"):
            DataSource(name="my source", type=SourceType.CSV)

    def test_rejects_special_chars(self) -> None:
        with pytest.raises(ValidationError, match="invalid"):
            DataSource(name="my@source", type=SourceType.CSV)

    def test_enabled_defaults_true(self) -> None:
        ds = make_data_source()
        assert ds.enabled is True

    def test_disabled_source(self) -> None:
        ds = make_data_source(enabled=False)
        assert ds.enabled is False

    def test_csv_type_with_config(self) -> None:
        ds = make_data_source(SourceType.CSV)
        assert ds.type == SourceType.CSV
        assert ds.csv is not None

    def test_tags_and_description(self) -> None:
        ds = make_data_source(
            tags=["sales", "daily"],
            description="Sales data from CSV",
        )
        assert ds.tags == ["sales", "daily"]
        assert ds.description == "Sales data from CSV"


@pytest.mark.unit
class TestSourcesConfig:
    def test_empty_sources(self) -> None:
        sc = make_sources_config(sources=[])
        assert sc.sources == []

    def test_get_source_found(self) -> None:
        sc = make_sources_config()
        assert sc.get_source("test_source") is not None

    def test_get_source_not_found(self) -> None:
        sc = make_sources_config()
        assert sc.get_source("nonexistent") is None

    def test_get_enabled_sources_filters_disabled(self) -> None:
        enabled = make_data_source(name="enabled_src")
        disabled = make_data_source(name="disabled_src", enabled=False)
        sc = make_sources_config(sources=[enabled, disabled])
        result = sc.get_enabled_sources()
        assert len(result) == 1
        assert result[0].name == "enabled_src"

    def test_all_disabled(self) -> None:
        d1 = make_data_source(name="a", enabled=False)
        d2 = make_data_source(name="b", enabled=False)
        sc = make_sources_config(sources=[d1, d2])
        assert sc.get_enabled_sources() == []

    def test_default_version(self) -> None:
        sc = make_sources_config()
        assert sc.version == "1.0"


@pytest.mark.unit
class TestPlatformSettings:
    def test_all_defaults(self) -> None:
        ps = PlatformSettings()
        assert ps.duckdb_path == "./data/warehouse.duckdb"
        assert ps.dbt_project_dir == "./dbt"
        assert ps.data_dir == "./data"
        assert ps.port == 8800
        assert ps.metabase_port == 3000
        assert ps.dbt_docs_port == 8081
        assert ps.auto_sync is True
        assert ps.auto_dbt is True
        assert ps.debounce_seconds == 600

    def test_custom_ports(self) -> None:
        ps = PlatformSettings(port=9000, metabase_port=4000)
        assert ps.port == 9000
        assert ps.metabase_port == 4000

    def test_auto_sync_toggle(self) -> None:
        ps = PlatformSettings(auto_sync=False)
        assert ps.auto_sync is False


@pytest.mark.unit
class TestDangoConfig:
    def test_minimal_just_project(self) -> None:
        ctx = make_project_context()
        config = DangoConfig(project=ctx)
        assert config.project.name == "Test Analytics"
        assert config.sources.sources == []
        assert config.platform.port == 8800

    def test_full_config(self) -> None:
        config = make_dango_config()
        assert config.project is not None
        assert config.sources is not None
        assert config.platform is not None

    def test_missing_project_raises(self) -> None:
        with pytest.raises(ValidationError):
            DangoConfig()
