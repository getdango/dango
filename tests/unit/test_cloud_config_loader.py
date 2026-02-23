"""tests/unit/test_cloud_config_loader.py

Tests for ConfigLoader.load_cloud_config() and save_cloud_config().
"""

import pytest
import yaml

from dango.config.exceptions import ConfigValidationError
from dango.config.loader import ConfigLoader
from dango.config.models import CloudConfig
from tests.factories.config_factories import make_cloud_config


@pytest.mark.unit
class TestLoadCloudConfig:
    def test_returns_none_when_file_missing(self, tmp_path):
        """load_cloud_config returns None when cloud.yml does not exist."""
        loader = ConfigLoader(project_root=tmp_path)
        result = loader.load_cloud_config()
        assert result is None

    def test_returns_cloud_config_when_valid(self, tmp_path):
        """load_cloud_config parses a valid cloud.yml into a CloudConfig."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        cloud_file = dango_dir / "cloud.yml"
        cloud_file.write_text("region: sfo3\nsize: s-4vcpu-8gb\n")

        loader = ConfigLoader(project_root=tmp_path)
        result = loader.load_cloud_config()

        assert isinstance(result, CloudConfig)
        assert result.region == "sfo3"
        assert result.size == "s-4vcpu-8gb"

    def test_invalid_yaml_raises_config_validation_error(self, tmp_path):
        """load_cloud_config raises ConfigValidationError for invalid YAML content."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        cloud_file = dango_dir / "cloud.yml"
        # Invalid field that CloudConfig does not accept
        cloud_file.write_text("droplet_id: not-an-integer\n")

        loader = ConfigLoader(project_root=tmp_path)
        with pytest.raises(ConfigValidationError):
            loader.load_cloud_config()

    def test_empty_file_returns_defaults(self, tmp_path):
        """load_cloud_config returns a CloudConfig with defaults when file is empty."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        cloud_file = dango_dir / "cloud.yml"
        cloud_file.write_text("")

        loader = ConfigLoader(project_root=tmp_path)
        result = loader.load_cloud_config()

        assert isinstance(result, CloudConfig)
        assert result.region == "nyc1"
        assert result.size == "s-2vcpu-4gb"


@pytest.mark.unit
class TestSaveCloudConfig:
    def test_creates_file(self, tmp_path):
        """save_cloud_config writes cloud.yml to the .dango directory."""
        loader = ConfigLoader(project_root=tmp_path)
        config = make_cloud_config()
        loader.save_cloud_config(config)

        cloud_file = tmp_path / ".dango" / "cloud.yml"
        assert cloud_file.exists()

    def test_round_trip(self, tmp_path):
        """save_cloud_config followed by load_cloud_config returns equivalent values."""
        loader = ConfigLoader(project_root=tmp_path)
        original = make_cloud_config(region="ams3", size="s-4vcpu-8gb")
        loader.save_cloud_config(original)

        loaded = loader.load_cloud_config()
        assert loaded is not None
        assert loaded.region == "ams3"
        assert loaded.size == "s-4vcpu-8gb"

    def test_excludes_none_fields(self, tmp_path):
        """save_cloud_config omits None fields from the written YAML."""
        loader = ConfigLoader(project_root=tmp_path)
        config = make_cloud_config()  # droplet_id, droplet_ip, domain all None
        loader.save_cloud_config(config)

        cloud_file = tmp_path / ".dango" / "cloud.yml"
        with open(cloud_file) as f:
            data: dict = yaml.safe_load(f) or {}

        assert "droplet_id" not in data
        assert "droplet_ip" not in data
        assert "domain" not in data
