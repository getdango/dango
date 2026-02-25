"""tests/integration/test_digitalocean.py

Cloud integration tests for DigitalOcean API client and Spaces operations.

Requires DIGITALOCEAN_TOKEN env var. Spaces tests additionally require
SPACES_ACCESS_KEY and SPACES_SECRET_KEY. Creates real DO resources and
cleans them up in finally blocks.

Cost: ~$0.01/run (cheapest droplet for ~2 minutes + API calls).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from typing import Any

import pytest

from dango.platform.cloud.digitalocean import DigitalOceanClient
from dango.platform.cloud.firewall import (
    add_allowed_ip,
    allow_all_web,
    create_default_firewall,
    delete_firewall,
    get_firewall_rules,
)
from dango.platform.cloud.provisioning import wait_for_droplet_ready
from dango.platform.cloud.spaces import SpacesClient

# Cheapest droplet size for testing
_TEST_SIZE = "s-1vcpu-512mb-10gb"
_TEST_REGION = "nyc1"
_TEST_IMAGE = "ubuntu-22-04-x64"


# ---------------------------------------------------------------------------
# Droplet lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.cloud
class TestDropletLifecycle:
    """Create, inspect, and delete a droplet end-to-end."""

    def test_create_get_delete(
        self,
        do_client: DigitalOceanClient,
        unique_test_name: str,
    ) -> None:
        """Full droplet CRUD lifecycle in a single test for ordering safety."""
        droplet_id = None
        try:
            # Create
            droplet = do_client.create_droplet(
                name=unique_test_name,
                region=_TEST_REGION,
                size=_TEST_SIZE,
                image=_TEST_IMAGE,
                tags=["dango-test"],
            )
            droplet_id = droplet["id"]
            assert droplet_id > 0
            assert droplet["name"] == unique_test_name

            # Wait for active
            active = wait_for_droplet_ready(do_client, droplet_id, timeout=180.0)
            assert active["status"] == "active"

            # Extract public IP
            networks = active.get("networks", {})
            public_ips = [
                n["ip_address"] for n in networks.get("v4", []) if n.get("type") == "public"
            ]
            assert len(public_ips) >= 1, "Droplet has no public IPv4"

            # Get droplet
            fetched = do_client.get_droplet(droplet_id)
            assert fetched["id"] == droplet_id
            assert fetched["region"]["slug"] == _TEST_REGION

            # List droplets by tag
            tagged = do_client.list_droplets(tag_name="dango-test")
            ids = [d["id"] for d in tagged]
            assert droplet_id in ids

        finally:
            if droplet_id is not None:
                do_client.delete_droplet(droplet_id)


# ---------------------------------------------------------------------------
# SSH key lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.cloud
class TestSSHKeyLifecycle:
    """Upload, list, and delete an SSH key end-to-end."""

    def test_upload_list_delete(
        self,
        do_client: DigitalOceanClient,
        unique_test_name: str,
        tmp_path: Any,
    ) -> None:
        """Full SSH key CRUD lifecycle."""
        from dango.platform.cloud.ssh import SSHManager

        key_id = None
        try:
            # Generate a test key pair
            key_path = tmp_path / "test_key"
            ssh = SSHManager(key_path=key_path)
            public_key = ssh.generate_key_pair()

            # Upload — upload_ssh_key() returns the unwrapped ssh_key dict
            ssh_key = do_client.upload_ssh_key(unique_test_name, public_key)
            key_id = ssh_key["id"]
            assert key_id > 0
            assert ssh_key["name"] == unique_test_name

            # List and verify present
            all_keys = do_client.list_ssh_keys()
            key_ids = [k["id"] for k in all_keys]
            assert key_id in key_ids

        finally:
            if key_id is not None:
                do_client.delete_ssh_key(key_id)


# ---------------------------------------------------------------------------
# Firewall lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.cloud
class TestFirewallLifecycle:
    """Create, update, and delete a firewall with a real droplet."""

    def test_create_update_delete(
        self,
        do_client: DigitalOceanClient,
        unique_test_name: str,
    ) -> None:
        """Full firewall CRUD lifecycle.

        DO API requires a real droplet_id for firewall creation, so we
        create a temporary droplet as well.
        """
        droplet_id = None
        firewall_id = None

        try:
            # Create a temporary droplet for firewall attachment
            droplet = do_client.create_droplet(
                name=unique_test_name,
                region=_TEST_REGION,
                size=_TEST_SIZE,
                image=_TEST_IMAGE,
                tags=["dango-test"],
            )
            droplet_id = droplet["id"]
            wait_for_droplet_ready(do_client, droplet_id, timeout=180.0)

            # Create default firewall
            fw = create_default_firewall(do_client, droplet_id)
            firewall_id = fw["id"]
            assert firewall_id

            # Get firewall rules — verify SSH, HTTP, HTTPS present
            fw_data = get_firewall_rules(do_client, firewall_id)
            inbound = fw_data.get("inbound_rules", [])
            inbound_ports = {r.get("ports") for r in inbound}
            assert "22" in inbound_ports, "SSH rule missing"
            assert "80" in inbound_ports, "HTTP rule missing"
            assert "443" in inbound_ports, "HTTPS rule missing"

            # Add allowed IP — switches to allowlist mode
            updated = add_allowed_ip(do_client, firewall_id, "10.0.0.1")
            inbound = updated.get("inbound_rules", [])
            web_rules = [r for r in inbound if r.get("ports") in ("80", "443")]
            for rule in web_rules:
                sources = rule.get("sources", {}).get("addresses", [])
                assert "10.0.0.1/32" in sources

            # Revert to allow all web
            reverted = allow_all_web(do_client, firewall_id)
            inbound = reverted.get("inbound_rules", [])
            web_rules = [r for r in inbound if r.get("ports") in ("80", "443")]
            for rule in web_rules:
                sources = rule.get("sources", {}).get("addresses", [])
                assert "0.0.0.0/0" in sources

        finally:
            if firewall_id is not None:
                try:
                    delete_firewall(do_client, firewall_id)
                except Exception:
                    pass
            if droplet_id is not None:
                try:
                    do_client.delete_droplet(droplet_id)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Spaces operations
# ---------------------------------------------------------------------------


@pytest.mark.cloud
class TestSpacesOperations:
    """Upload, download, list, and delete objects in DigitalOcean Spaces."""

    @pytest.fixture(scope="class")
    def spaces_client(
        self, require_spaces_env: tuple[str, str]
    ) -> Generator[SpacesClient, None, None]:
        """Create a SpacesClient with a temporary bucket for testing."""
        access_key, secret_key = require_spaces_env
        bucket = f"dango-test-{uuid.uuid4().hex[:8]}"
        client = SpacesClient(
            bucket=bucket,
            region="nyc3",
            access_key=access_key,
            secret_key=secret_key,
        )
        client.create_bucket()
        yield client
        # Teardown: clean up all objects and delete bucket
        try:
            for obj in client.list_objects():
                client.delete(obj["Key"])
            client.delete_bucket()
        except Exception:
            pass  # best-effort cleanup

    def test_upload_download_list_delete(self, spaces_client: SpacesClient) -> None:
        """Full object lifecycle: upload → download → list → delete."""
        test_key = f"test/{uuid.uuid4().hex[:8]}.txt"
        test_data = b"Hello from Dango cloud integration tests!"

        # Upload
        spaces_client.upload(test_key, test_data)

        # Download and verify
        downloaded = spaces_client.download(test_key)
        assert downloaded == test_data

        # List with prefix
        objects = spaces_client.list_objects(prefix="test/")
        keys = [obj["Key"] for obj in objects]
        assert test_key in keys

        # Exists
        assert spaces_client.exists(test_key) is True

        # Delete
        spaces_client.delete(test_key)

        # Verify deleted
        assert spaces_client.exists(test_key) is False

    def test_nonexistent_object(self, spaces_client: SpacesClient) -> None:
        """exists() returns False for unknown keys."""
        assert spaces_client.exists("nonexistent/key/that/does/not/exist.bin") is False
