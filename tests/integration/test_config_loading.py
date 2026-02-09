"""Integration tests for full config loading from disk."""

import pytest

from dango.config.loader import ConfigLoader
from dango.config.models import DangoConfig


class TestConfigLoading:
    @pytest.mark.integration
    def test_load_config_from_valid_project(self, tmp_project_dir):
        """Loading config from a valid project directory produces a DangoConfig."""
        loader = ConfigLoader(project_root=tmp_project_dir)
        config = loader.load_config()

        assert isinstance(config, DangoConfig)
        assert config.project.name == "Test Project"
        assert config.project.created_by == "test@example.com"
        assert len(config.sources.sources) == 1
        assert config.sources.sources[0].name == "test_csv"
