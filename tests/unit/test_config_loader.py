"""Tests for dango.config.loader — ConfigLoader path setup and YAML loading."""

import pytest

from dango.config.exceptions import ConfigNotFoundError
from dango.config.loader import ConfigLoader


@pytest.mark.unit
class TestConfigLoaderPaths:
    def test_config_loader_sets_paths_from_project_root(self, tmp_path):
        """ConfigLoader derives .dango dir and config file paths from project_root."""
        loader = ConfigLoader(project_root=tmp_path)
        assert loader.project_root == tmp_path
        assert loader.dango_dir == tmp_path / ".dango"
        assert loader.project_file == tmp_path / ".dango" / "project.yml"
        assert loader.sources_file == tmp_path / ".dango" / "sources.yml"


@pytest.mark.unit
class TestLoadYaml:
    def test_load_yaml_raises_on_missing_file(self, tmp_path):
        """load_yaml raises ConfigNotFoundError for a nonexistent file."""
        loader = ConfigLoader(project_root=tmp_path)
        missing = tmp_path / "nonexistent.yml"
        with pytest.raises(ConfigNotFoundError):
            loader.load_yaml(missing)
