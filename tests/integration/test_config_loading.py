"""Integration tests for full config loading from disk."""

import pytest
import yaml

from dango.config.credentials import CredentialManager
from dango.config.loader import ConfigLoader
from dango.config.models import DangoConfig, SourceType
from tests.factories.config_factories import make_dango_config, make_data_source


@pytest.mark.integration
class TestConfigLoading:
    def test_load_config_from_valid_project(self, tmp_project_dir):
        loader = ConfigLoader(project_root=tmp_project_dir)
        config = loader.load_config()

        assert isinstance(config, DangoConfig)
        assert config.project.name == "Test Project"
        assert config.project.created_by == "test@example.com"
        assert len(config.sources.sources) == 1
        assert config.sources.sources[0].name == "test_csv"

    def test_round_trip_save_load(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        original = make_dango_config()
        loader.save_config(original)

        loaded = loader.load_config()
        assert loaded.project.name == original.project.name
        assert len(loaded.sources.sources) == len(original.sources.sources)

    def test_load_with_platform_settings(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        project_data = {
            "project": {
                "name": "P",
                "created_by": "a@b.com",
                "purpose": "test",
            },
            "platform": {
                "port": 9999,
                "metabase_port": 4000,
            },
        }
        (dango_dir / "project.yml").write_text(yaml.safe_dump(project_data))

        loader = ConfigLoader(project_root=tmp_path)
        config = loader.load_config()
        assert config.platform.port == 9999
        assert config.platform.metabase_port == 4000

    def test_multiple_source_types(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        project_data = {
            "project": {
                "name": "Multi",
                "created_by": "a@b.com",
                "purpose": "test",
            },
        }
        sources_data = {
            "version": "1.0",
            "sources": [
                {"name": "csv_src", "type": "csv", "csv": {"directory": "./data"}},
                {
                    "name": "sheets_src",
                    "type": "google_sheets",
                    "google_sheets": {
                        "spreadsheet_url_or_id": "abc123",
                        "range_names": ["Sheet1"],
                    },
                },
            ],
        }
        (dango_dir / "project.yml").write_text(yaml.safe_dump(project_data))
        (dango_dir / "sources.yml").write_text(yaml.safe_dump(sources_data))

        loader = ConfigLoader(project_root=tmp_path)
        config = loader.load_config()
        assert len(config.sources.sources) == 2
        types = {s.type for s in config.sources.sources}
        assert SourceType.CSV in types
        assert SourceType.GOOGLE_SHEETS in types

    def test_validate_config_valid(self, tmp_project_dir):
        loader = ConfigLoader(project_root=tmp_project_dir)
        is_valid, errors = loader.validate_config()
        assert is_valid is True
        assert errors == []

    def test_validate_config_corrupt(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "project.yml").write_text("project:\n  name: [broken\n")
        loader = ConfigLoader(project_root=tmp_path)
        is_valid, errors = loader.validate_config()
        assert is_valid is False


@pytest.mark.integration
class TestCredentialManagerIntegration:
    def test_full_lifecycle(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.init_dlt_directory()

        # Save credentials
        cm.save_secrets({"sources": {"google": {"client_id": "abc"}}})
        assert cm.has_credentials("google") is True

        # Load credentials
        creds = cm.get_source_credentials("google")
        assert creds["client_id"] == "abc"

        # Delete credentials
        cm.delete_source_credentials("google")
        assert cm.has_credentials("google") is False

    def test_gitignore_exclusions(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.init_dlt_directory()

        gitignore = (tmp_path / ".gitignore").read_text()
        assert ".dlt/secrets.toml" in gitignore
        assert ".dlt/*.db" in gitignore
