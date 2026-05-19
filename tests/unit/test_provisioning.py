"""tests/unit/test_provisioning.py

Unit tests for dango/platform/cloud/provisioning.py.

All external calls (DO API, socket, time.sleep) are mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudError
from dango.platform.cloud.provisioning import (
    BUDGET_TIER,
    DEFAULT_TIER,
    PERFORMANCE_TIER,
    SIZE_TIERS,
    STANDARD_TIER,
    _extract_public_ipv4,
    get_region_info,
    get_size_tier,
    list_regions,
    provision_droplet,
    save_provisioning_metadata,
    suggest_nearest_region,
    validate_custom_size,
    wait_for_droplet_ready,
    wait_for_ssh,
)

# ---------------------------------------------------------------------------
# 1. DropletSizeTier
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDropletSizeTier:
    def test_budget_tier_values(self):
        """Budget tier has correct slug, vCPUs, RAM, and price."""
        assert BUDGET_TIER.slug == "s-1vcpu-2gb"
        assert BUDGET_TIER.vcpus == 1
        assert BUDGET_TIER.ram_gb == 2
        assert BUDGET_TIER.disk_gb == 50
        assert BUDGET_TIER.price_monthly == 12
        assert BUDGET_TIER.warning is not None

    def test_standard_tier_is_default(self):
        """DEFAULT_TIER and STANDARD_TIER are the same object."""
        assert DEFAULT_TIER is STANDARD_TIER

    def test_standard_tier_values(self):
        """Standard tier has correct values and no warning."""
        assert STANDARD_TIER.slug == "s-2vcpu-4gb"
        assert STANDARD_TIER.vcpus == 2
        assert STANDARD_TIER.ram_gb == 4
        assert STANDARD_TIER.disk_gb == 80
        assert STANDARD_TIER.price_monthly == 24
        assert STANDARD_TIER.warning is None

    def test_performance_tier_values(self):
        """Performance tier has correct values."""
        assert PERFORMANCE_TIER.slug == "s-4vcpu-8gb"
        assert PERFORMANCE_TIER.vcpus == 4
        assert PERFORMANCE_TIER.ram_gb == 8
        assert PERFORMANCE_TIER.disk_gb == 160
        assert PERFORMANCE_TIER.price_monthly == 48

    def test_size_tiers_list_has_two_entries(self):
        """SIZE_TIERS contains exactly two tiers (Standard, Performance)."""
        assert len(SIZE_TIERS) == 2
        slugs = [t.slug for t in SIZE_TIERS]
        assert "s-2vcpu-4gb" in slugs
        assert "s-4vcpu-8gb" in slugs

    def test_get_size_tier_known_slug(self):
        """get_size_tier returns the correct tier for a known slug."""
        tier = get_size_tier("s-2vcpu-4gb")
        assert tier is STANDARD_TIER

    def test_get_size_tier_unknown_slug_returns_none(self):
        """get_size_tier returns None for an unknown slug."""
        assert get_size_tier("s-99vcpu-999gb") is None

    def test_validate_custom_size_valid_slugs(self):
        """validate_custom_size accepts known DO slug prefixes."""
        assert validate_custom_size("s-1vcpu-1gb") is True
        assert validate_custom_size("g-2vcpu-8gb") is True
        assert validate_custom_size("so1_5-2vcpu-16gb") is True
        assert validate_custom_size("c-4") is True
        assert validate_custom_size("m-2vcpu-16gb") is True

    def test_validate_custom_size_invalid(self):
        """validate_custom_size rejects empty strings and bad prefixes."""
        assert validate_custom_size("") is False
        assert validate_custom_size("invalid-slug") is False
        assert validate_custom_size("2vcpu") is False

    def test_tier_is_frozen(self):
        """DropletSizeTier instances are immutable (frozen dataclass)."""
        with pytest.raises((AttributeError, TypeError)):
            STANDARD_TIER.vcpus = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. RegionInfo
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegionInfo:
    def test_region_count(self):
        """list_regions returns exactly 10 regions."""
        assert len(list_regions()) == 10

    def test_gdpr_regions(self):
        """ams3 and fra1 are flagged as GDPR regions."""
        ams3 = get_region_info("ams3")
        fra1 = get_region_info("fra1")
        assert ams3 is not None and ams3.gdpr is True
        assert fra1 is not None and fra1.gdpr is True

    def test_non_gdpr_regions(self):
        """nyc1 and sgp1 are not GDPR regions."""
        nyc1 = get_region_info("nyc1")
        sgp1 = get_region_info("sgp1")
        assert nyc1 is not None and nyc1.gdpr is False
        assert sgp1 is not None and sgp1.gdpr is False

    def test_get_region_info_known_slug(self):
        """get_region_info returns a RegionInfo for a known slug."""
        region = get_region_info("sfo3")
        assert region is not None
        assert region.slug == "sfo3"
        assert region.city == "San Francisco"

    def test_get_region_info_unknown_returns_none(self):
        """get_region_info returns None for an unknown slug."""
        assert get_region_info("xyz99") is None

    def test_list_regions_returns_all(self):
        """list_regions includes nyc1, ams3, syd1 (spot check)."""
        slugs = {r.slug for r in list_regions()}
        assert "nyc1" in slugs
        assert "ams3" in slugs
        assert "syd1" in slugs

    def test_region_is_frozen(self):
        """RegionInfo instances are immutable (frozen dataclass)."""
        nyc1 = get_region_info("nyc1")
        assert nyc1 is not None
        with pytest.raises((AttributeError, TypeError)):
            nyc1.city = "Boston"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 3. suggest_nearest_region
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSuggestNearestRegion:
    def test_us_east_timezone_suggests_nyc(self):
        """UTC-5 (US East) maps to nyc1 or nyc3 (utc_offset = -5)."""
        # timezone is seconds WEST of UTC, so EST = +18000
        with patch("dango.platform.cloud.provisioning.time") as mock_time:
            mock_time.localtime.return_value.tm_isdst = 0
            mock_time.timezone = 18000
            result = suggest_nearest_region()
        assert result.utc_offset == -5.0

    def test_us_west_timezone_suggests_sfo(self):
        """UTC-8 (US West) maps to sfo3 (utc_offset = -8)."""
        with patch("dango.platform.cloud.provisioning.time") as mock_time:
            mock_time.localtime.return_value.tm_isdst = 0
            mock_time.timezone = 28800
            result = suggest_nearest_region()
        assert result.slug == "sfo3"

    def test_singapore_timezone_suggests_sgp(self):
        """UTC+8 maps to sgp1 (utc_offset = +8)."""
        with patch("dango.platform.cloud.provisioning.time") as mock_time:
            mock_time.localtime.return_value.tm_isdst = 0
            mock_time.timezone = -28800
            result = suggest_nearest_region()
        assert result.slug == "sgp1"

    def test_exception_falls_back_to_nyc1(self):
        """Any exception during offset calculation falls back to nyc1."""
        with patch("dango.platform.cloud.provisioning.time") as mock_time:
            mock_time.localtime.return_value.tm_isdst = 0
            # -"bad" raises TypeError (unary minus on str) → caught by except Exception
            mock_time.timezone = "bad"
            result = suggest_nearest_region()
        assert result.slug == "nyc1"

    def test_daylight_saving_uses_altzone(self):
        """When DST is currently active, altzone is used for local offset."""
        # UTC+1 (Amsterdam summer time = CEST, altzone = -3600).
        # timezone set to sfo3 equivalent so a wrong branch gives utc_offset=-8, not +1.
        with patch("dango.platform.cloud.provisioning.time") as mock_time:
            mock_time.localtime.return_value.tm_isdst = 1
            mock_time.altzone = -3600  # DST branch: UTC+1 → ams3/fra1
            mock_time.timezone = 28800  # non-DST branch: UTC-8 → sfo3 (wrong result)
            result = suggest_nearest_region()
        # ams3 and fra1 both at +1
        assert result.utc_offset == 1.0


# ---------------------------------------------------------------------------
# 4. wait_for_droplet_ready
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWaitForDropletReady:
    def test_already_active(self):
        """Droplet that is already active returns immediately."""
        client = MagicMock()
        active = {"id": 1, "status": "active"}
        client.get_droplet.return_value = active

        with patch("dango.platform.cloud.provisioning.time.sleep") as mock_sleep:
            result = wait_for_droplet_ready(client, 1, poll_interval=5.0, timeout=30.0)

        assert result == active
        mock_sleep.assert_not_called()

    def test_polls_until_active(self):
        """Droplet transitions from 'new' → 'new' → 'active'."""
        client = MagicMock()
        client.get_droplet.side_effect = [
            {"id": 1, "status": "new"},
            {"id": 1, "status": "new"},
            {"id": 1, "status": "active"},
        ]

        with patch("dango.platform.cloud.provisioning.time.sleep") as mock_sleep:
            with patch("dango.platform.cloud.provisioning.time.monotonic") as mock_mono:
                mock_mono.side_effect = [0.0, 0.0, 5.0, 10.0, 15.0, 20.0]
                result = wait_for_droplet_ready(client, 1, poll_interval=5.0, timeout=120.0)

        assert result["status"] == "active"
        assert mock_sleep.call_count == 2

    def test_errored_state_raises(self):
        """Droplet entering 'errored' state raises CloudError D010."""
        client = MagicMock()
        client.get_droplet.return_value = {"id": 1, "status": "errored"}

        with pytest.raises(CloudError) as exc_info:
            wait_for_droplet_ready(client, 1, poll_interval=1.0, timeout=30.0)

        assert exc_info.value.error_code == "DANGO-D010"

    def test_archive_state_raises(self):
        """Droplet entering 'archive' state raises CloudError D010."""
        client = MagicMock()
        client.get_droplet.return_value = {"id": 1, "status": "archive"}

        with pytest.raises(CloudError) as exc_info:
            wait_for_droplet_ready(client, 1, poll_interval=1.0, timeout=30.0)

        assert exc_info.value.error_code == "DANGO-D010"

    def test_timeout_raises(self):
        """CloudError D011 raised when timeout elapses."""
        client = MagicMock()
        client.get_droplet.return_value = {"id": 1, "status": "new"}

        with patch("dango.platform.cloud.provisioning.time.sleep"):
            with patch("dango.platform.cloud.provisioning.time.monotonic") as mock_mono:
                # deadline = start + timeout; first check passes, second exceeds deadline
                mock_mono.side_effect = [0.0, 0.0, 200.0]
                with pytest.raises(CloudError) as exc_info:
                    wait_for_droplet_ready(client, 1, poll_interval=5.0, timeout=120.0)

        assert exc_info.value.error_code == "DANGO-D011"


# ---------------------------------------------------------------------------
# 5. wait_for_ssh
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWaitForSSH:
    def test_immediate_success(self):
        """Returns immediately if SSH is reachable on first attempt."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("dango.platform.cloud.provisioning.socket.create_connection") as mock_create:
            mock_create.return_value = mock_conn
            with patch("dango.platform.cloud.provisioning.time.sleep") as mock_sleep:
                wait_for_ssh("1.2.3.4", max_attempts=3, attempt_interval=1.0)

        mock_sleep.assert_not_called()
        mock_create.assert_called_once_with(("1.2.3.4", 22), timeout=5.0)

    def test_retries_then_succeeds(self):
        """Retries on OSError, succeeds on second attempt."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("dango.platform.cloud.provisioning.socket.create_connection") as mock_create:
            mock_create.side_effect = [OSError("refused"), mock_conn]
            with patch("dango.platform.cloud.provisioning.time.sleep") as mock_sleep:
                wait_for_ssh("1.2.3.4", max_attempts=3, attempt_interval=2.0)

        mock_sleep.assert_called_once_with(2.0)

    def test_all_attempts_fail_raises(self):
        """CloudError D012 raised when all attempts fail."""
        with patch("dango.platform.cloud.provisioning.socket.create_connection") as mock_create:
            mock_create.side_effect = OSError("refused")
            with patch("dango.platform.cloud.provisioning.time.sleep"):
                with pytest.raises(CloudError) as exc_info:
                    wait_for_ssh("1.2.3.4", max_attempts=3, attempt_interval=1.0)

        assert exc_info.value.error_code == "DANGO-D012"


# ---------------------------------------------------------------------------
# 6. _extract_public_ipv4
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractPublicIpv4:
    def test_extracts_public_ip(self):
        """Extracts the public IPv4 from network data."""
        droplet: dict = {
            "networks": {
                "v4": [
                    {"type": "private", "ip_address": "10.0.0.1"},
                    {"type": "public", "ip_address": "1.2.3.4"},
                ]
            }
        }
        assert _extract_public_ipv4(droplet) == "1.2.3.4"

    def test_returns_none_when_no_public(self):
        """Returns None when only private networks exist."""
        droplet: dict = {"networks": {"v4": [{"type": "private", "ip_address": "10.0.0.1"}]}}
        assert _extract_public_ipv4(droplet) is None

    def test_returns_none_when_no_networks(self):
        """Returns None when networks key is missing."""
        assert _extract_public_ipv4({}) is None

    def test_returns_none_when_ip_address_is_none(self):
        """Returns None when ip_address field is None (not just absent)."""
        droplet: dict = {"networks": {"v4": [{"type": "public", "ip_address": None}]}}
        assert _extract_public_ipv4(droplet) is None


# ---------------------------------------------------------------------------
# 7. provision_droplet
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProvisionDroplet:
    def _active_droplet(self, droplet_id: int = 42, ip: str = "1.2.3.4") -> dict:
        return {
            "id": droplet_id,
            "status": "active",
            "networks": {"v4": [{"type": "public", "ip_address": ip}]},
        }

    def test_full_success_flow(self):
        """provision_droplet returns the active droplet on success."""
        client = MagicMock()
        new_droplet = {"id": 42, "status": "new"}
        active = self._active_droplet()
        client.create_droplet.return_value = new_droplet

        with patch("dango.platform.cloud.provisioning.wait_for_droplet_ready") as mock_poll:
            mock_poll.return_value = active
            with patch("dango.platform.cloud.provisioning.wait_for_ssh") as mock_ssh:
                result = provision_droplet(
                    client, "test-node", "nyc1", "s-2vcpu-4gb", ssh_key_ids=[100]
                )

        assert result["id"] == 42
        mock_ssh.assert_called_once_with("1.2.3.4")

    def test_always_adds_dango_tag(self):
        """provision_droplet always includes the 'dango' tag."""
        client = MagicMock()
        client.create_droplet.return_value = {"id": 1, "status": "new"}
        active = self._active_droplet(1)

        with patch("dango.platform.cloud.provisioning.wait_for_droplet_ready") as mock_poll:
            mock_poll.return_value = active
            with patch("dango.platform.cloud.provisioning.wait_for_ssh"):
                provision_droplet(client, "n", "nyc1", "s-2vcpu-4gb", ssh_key_ids=[])

        call_kwargs = client.create_droplet.call_args[1]
        assert "dango" in call_kwargs["tags"]

    def test_extra_tags_appended(self):
        """extra_tags are appended alongside the 'dango' tag."""
        client = MagicMock()
        client.create_droplet.return_value = {"id": 1, "status": "new"}
        active = self._active_droplet(1)

        with patch("dango.platform.cloud.provisioning.wait_for_droplet_ready") as mock_poll:
            mock_poll.return_value = active
            with patch("dango.platform.cloud.provisioning.wait_for_ssh"):
                provision_droplet(
                    client, "n", "nyc1", "s-2vcpu-4gb", ssh_key_ids=[], extra_tags=["prod", "web"]
                )

        tags = client.create_droplet.call_args[1]["tags"]
        assert "dango" in tags
        assert "prod" in tags
        assert "web" in tags

    def test_cleanup_on_poll_failure(self):
        """Droplet is deleted when polling fails."""
        client = MagicMock()
        client.create_droplet.return_value = {"id": 42, "status": "new"}

        with patch("dango.platform.cloud.provisioning.wait_for_droplet_ready") as mock_poll:
            mock_poll.side_effect = CloudError("timeout", error_code="DANGO-D011")
            with pytest.raises(CloudError):
                provision_droplet(client, "n", "nyc1", "s-2vcpu-4gb", ssh_key_ids=[])

        client.delete_droplet.assert_called_once_with(42)

    def test_cleanup_on_ssh_failure(self):
        """Droplet is deleted when SSH check fails."""
        client = MagicMock()
        client.create_droplet.return_value = {"id": 42, "status": "new"}
        active = self._active_droplet()

        with patch("dango.platform.cloud.provisioning.wait_for_droplet_ready") as mock_poll:
            mock_poll.return_value = active
            with patch("dango.platform.cloud.provisioning.wait_for_ssh") as mock_ssh:
                mock_ssh.side_effect = CloudError("no ssh", error_code="DANGO-D012")
                with pytest.raises(CloudError) as exc_info:
                    provision_droplet(client, "n", "nyc1", "s-2vcpu-4gb", ssh_key_ids=[])

        client.delete_droplet.assert_called_once_with(42)
        assert exc_info.value.error_code == "DANGO-D012"

    def test_cleanup_on_no_ip(self):
        """Droplet is deleted when no public IPv4 is found."""
        client = MagicMock()
        client.create_droplet.return_value = {"id": 42, "status": "new"}
        active_no_ip = {"id": 42, "status": "active", "networks": {"v4": []}}

        with patch("dango.platform.cloud.provisioning.wait_for_droplet_ready") as mock_poll:
            mock_poll.return_value = active_no_ip
            with pytest.raises(CloudError) as exc_info:
                provision_droplet(client, "n", "nyc1", "s-2vcpu-4gb", ssh_key_ids=[])

        client.delete_droplet.assert_called_once_with(42)
        assert exc_info.value.error_code == "DANGO-D013"

    def test_cleanup_failure_swallowed(self):
        """Cleanup errors are swallowed and original error is re-raised."""
        client = MagicMock()
        client.create_droplet.return_value = {"id": 42, "status": "new"}
        client.delete_droplet.side_effect = Exception("delete failed")

        with patch("dango.platform.cloud.provisioning.wait_for_droplet_ready") as mock_poll:
            mock_poll.side_effect = CloudError("timeout", error_code="DANGO-D011")
            with pytest.raises(CloudError) as exc_info:
                provision_droplet(client, "n", "nyc1", "s-2vcpu-4gb", ssh_key_ids=[])

        # Original error propagated, not the delete error
        assert exc_info.value.error_code == "DANGO-D011"


# ---------------------------------------------------------------------------
# 8. save_provisioning_metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSaveProvisioningMetadata:
    def test_creates_new_config(self, tmp_path: Path):
        """Creates cloud.yml with provisioning metadata when none exists."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()

        save_provisioning_metadata(
            tmp_path,
            droplet_id=123,
            droplet_ip="5.6.7.8",
            region="sfo3",
            size="s-2vcpu-4gb",
        )

        from dango.config.loader import ConfigLoader

        loader = ConfigLoader(tmp_path)
        saved = loader.load_cloud_config()
        assert saved is not None
        assert saved.droplet_id == 123
        assert saved.droplet_ip == "5.6.7.8"
        assert saved.region == "sfo3"
        assert saved.size == "s-2vcpu-4gb"

    def test_updates_existing_config(self, tmp_path: Path):
        """Updates existing cloud.yml, preserving other fields."""
        from dango.config.loader import ConfigLoader
        from dango.config.models import CloudConfig

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        loader = ConfigLoader(tmp_path)
        loader.save_cloud_config(
            CloudConfig(region="nyc1", size="s-2vcpu-4gb", domain="example.com")
        )

        save_provisioning_metadata(
            tmp_path,
            droplet_id=999,
            droplet_ip="9.9.9.9",
            region="nyc1",
            size="s-4vcpu-8gb",
        )

        saved = loader.load_cloud_config()
        assert saved is not None
        assert saved.droplet_id == 999
        assert saved.droplet_ip == "9.9.9.9"
        assert saved.size == "s-4vcpu-8gb"
        assert saved.domain == "example.com"  # preserved
