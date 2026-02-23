"""tests/unit/test_cloud_config.py

Tests for CloudConfig, SpacesConfig, and DbtOverrides Pydantic models.
"""

import pytest
from pydantic import ValidationError

from dango.config.models import CloudConfig, DbtOverrides, SpacesConfig


@pytest.mark.unit
class TestCloudConfig:
    def test_defaults(self):
        """CloudConfig provides correct field defaults."""
        config = CloudConfig()
        assert config.region == "nyc1"
        assert config.size == "s-2vcpu-4gb"
        assert config.ssh_key_path == ".dango/cloud_key"
        assert config.droplet_id is None
        assert config.droplet_ip is None
        assert config.domain is None
        assert config.spaces is None
        assert config.dbt_overrides is None

    def test_all_fields(self):
        """CloudConfig accepts all optional fields."""
        config = CloudConfig(
            droplet_id=12345,
            droplet_ip="1.2.3.4",
            region="sfo3",
            size="s-4vcpu-8gb",
            domain="example.com",
            ssh_key_path=".dango/custom_key",
            spaces=SpacesConfig(bucket="my-bucket"),
            dbt_overrides=DbtOverrides(threads=4, memory_limit="2GB"),
        )
        assert config.droplet_id == 12345
        assert config.droplet_ip == "1.2.3.4"
        assert config.region == "sfo3"
        assert config.size == "s-4vcpu-8gb"
        assert config.domain == "example.com"
        assert config.ssh_key_path == ".dango/custom_key"
        assert config.spaces is not None
        assert config.dbt_overrides is not None

    def test_serialization_excludes_none(self):
        """model_dump with exclude_none=True omits unset optional fields."""
        config = CloudConfig(region="nyc1", size="s-2vcpu-4gb")
        data = config.model_dump(mode="json", exclude_none=True)
        assert "droplet_id" not in data
        assert "droplet_ip" not in data
        assert "domain" not in data
        assert "spaces" not in data
        assert "dbt_overrides" not in data
        assert data["region"] == "nyc1"
        assert data["size"] == "s-2vcpu-4gb"

    def test_round_trip(self):
        """CloudConfig survives a dump-then-construct round trip."""
        original = CloudConfig(
            droplet_id=99,
            droplet_ip="10.0.0.1",
            region="ams3",
            domain="my.example.com",
        )
        data = original.model_dump(mode="json", exclude_none=True)
        restored = CloudConfig(**data)
        assert restored.droplet_id == original.droplet_id
        assert restored.droplet_ip == original.droplet_ip
        assert restored.region == original.region
        assert restored.domain == original.domain


@pytest.mark.unit
class TestSpacesConfig:
    def test_bucket_required(self):
        """SpacesConfig requires a bucket name."""
        with pytest.raises(ValidationError):
            SpacesConfig()  # missing bucket

    def test_region_optional(self):
        """SpacesConfig region defaults to None."""
        config = SpacesConfig(bucket="my-bucket")
        assert config.bucket == "my-bucket"
        assert config.region is None

    def test_all_fields(self):
        """SpacesConfig accepts bucket and region."""
        config = SpacesConfig(bucket="my-bucket", region="nyc3")
        assert config.bucket == "my-bucket"
        assert config.region == "nyc3"


@pytest.mark.unit
class TestDbtOverrides:
    def test_all_optional(self):
        """DbtOverrides can be constructed with no arguments."""
        overrides = DbtOverrides()
        assert overrides.threads is None
        assert overrides.memory_limit is None

    def test_threads(self):
        """DbtOverrides accepts thread count override."""
        overrides = DbtOverrides(threads=8)
        assert overrides.threads == 8
        assert overrides.memory_limit is None

    def test_memory_limit(self):
        """DbtOverrides accepts memory limit override."""
        overrides = DbtOverrides(memory_limit="4GB")
        assert overrides.threads is None
        assert overrides.memory_limit == "4GB"
