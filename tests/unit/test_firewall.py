"""tests/unit/test_firewall.py

Unit tests for dango/platform/cloud/firewall.py.

All DO API client calls are mocked — no real network traffic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dango.exceptions import CloudError
from dango.platform.cloud.firewall import (
    DEFAULT_OUTBOUND_RULES,
    add_allowed_ip,
    allow_all_web,
    create_default_firewall,
    format_firewall_rules,
    restrict_web_to_ips,
    save_firewall_metadata,
    validate_ip_or_cidr,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PUBLIC_SOURCES = {"addresses": ["0.0.0.0/0", "::/0"]}
_SSH_RULE = {"protocol": "tcp", "ports": "22", "sources": _PUBLIC_SOURCES}
_HTTP_RULE_PUBLIC = {"protocol": "tcp", "ports": "80", "sources": _PUBLIC_SOURCES}
_HTTPS_RULE_PUBLIC = {"protocol": "tcp", "ports": "443", "sources": _PUBLIC_SOURCES}


def _make_public_firewall(
    fw_id: str = "fw-abc",
    droplet_ids: list[int] | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Return a firewall dict with all-public inbound rules."""
    return {
        "id": fw_id,
        "name": f"dango-fw-{fw_id}",
        "inbound_rules": [_SSH_RULE, _HTTP_RULE_PUBLIC, _HTTPS_RULE_PUBLIC],
        "outbound_rules": DEFAULT_OUTBOUND_RULES,
        "droplet_ids": droplet_ids or [42],
        "tags": tags or [],
    }


def _make_allowlist_firewall(
    cidrs: list[str],
    fw_id: str = "fw-abc",
    droplet_ids: list[int] | None = None,
) -> dict:
    """Return a firewall dict with allowlist inbound rules for ports 80 and 443."""
    sources = {"addresses": cidrs}
    return {
        "id": fw_id,
        "name": f"dango-fw-{fw_id}",
        "inbound_rules": [
            _SSH_RULE,
            {"protocol": "tcp", "ports": "80", "sources": sources},
            {"protocol": "tcp", "ports": "443", "sources": sources},
        ],
        "outbound_rules": DEFAULT_OUTBOUND_RULES,
        "droplet_ids": droplet_ids or [42],
        "tags": [],
    }


# ---------------------------------------------------------------------------
# 1. validate_ip_or_cidr
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidateIpOrCidr:
    def test_bare_ipv4_appends_slash32(self):
        """Bare IPv4 address is normalised to /32."""
        assert validate_ip_or_cidr("203.0.113.42") == "203.0.113.42/32"

    def test_valid_cidr(self):
        """Valid CIDR is accepted and returned as-is (after normalisation)."""
        assert validate_ip_or_cidr("203.0.113.0/24") == "203.0.113.0/24"

    def test_cidr_with_host_bits_normalised(self):
        """CIDR with host bits set is normalised to network address."""
        assert validate_ip_or_cidr("10.0.0.5/24") == "10.0.0.0/24"

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace is stripped."""
        assert validate_ip_or_cidr("  1.2.3.4  ") == "1.2.3.4/32"

    def test_invalid_ip_raises(self):
        """Invalid IP raises CloudError D020."""
        with pytest.raises(CloudError) as exc_info:
            validate_ip_or_cidr("999.999.999.999")
        assert exc_info.value.error_code == "DANGO-D020"

    def test_empty_string_raises(self):
        """Empty string raises CloudError D020."""
        with pytest.raises(CloudError) as exc_info:
            validate_ip_or_cidr("")
        assert exc_info.value.error_code == "DANGO-D020"

    def test_ipv6_rejected(self):
        """IPv6 addresses are rejected with CloudError D020."""
        with pytest.raises(CloudError) as exc_info:
            validate_ip_or_cidr("2001:db8::1")
        assert exc_info.value.error_code == "DANGO-D020"

    def test_slash32_cidr_is_valid(self):
        """An explicit /32 CIDR is valid."""
        assert validate_ip_or_cidr("1.2.3.4/32") == "1.2.3.4/32"


# ---------------------------------------------------------------------------
# 2. create_default_firewall
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateDefaultFirewall:
    def test_creates_with_correct_name(self):
        """Firewall name is dango-fw-{droplet_id}."""
        client = MagicMock()
        client.create_firewall.return_value = {"id": "fw-1", "name": "dango-fw-42"}

        create_default_firewall(client, droplet_id=42)

        client.create_firewall.assert_called_once()
        kwargs = client.create_firewall.call_args[1]
        assert kwargs["name"] == "dango-fw-42"

    def test_applied_to_droplet(self):
        """Firewall is associated with the given droplet_id."""
        client = MagicMock()
        client.create_firewall.return_value = {"id": "fw-1"}

        create_default_firewall(client, droplet_id=99)

        kwargs = client.create_firewall.call_args[1]
        assert kwargs["droplet_ids"] == [99]

    def test_includes_ssh_http_https_inbound(self):
        """Default firewall allows SSH, HTTP, and HTTPS inbound."""
        client = MagicMock()
        client.create_firewall.return_value = {"id": "fw-1"}

        create_default_firewall(client, droplet_id=1)

        kwargs = client.create_firewall.call_args[1]
        ports = {r["ports"] for r in kwargs["inbound_rules"]}
        assert "22" in ports
        assert "80" in ports
        assert "443" in ports

    def test_inbound_rules_open_to_all(self):
        """All default inbound rules include 0.0.0.0/0 in sources."""
        client = MagicMock()
        client.create_firewall.return_value = {"id": "fw-1"}

        create_default_firewall(client, droplet_id=1)

        kwargs = client.create_firewall.call_args[1]
        for rule in kwargs["inbound_rules"]:
            addresses = rule["sources"]["addresses"]
            assert "0.0.0.0/0" in addresses

    def test_returns_firewall_dict(self):
        """create_default_firewall returns the client's response."""
        client = MagicMock()
        fw = {"id": "fw-abc", "name": "dango-fw-42"}
        client.create_firewall.return_value = fw

        result = create_default_firewall(client, droplet_id=42)
        assert result == fw


# ---------------------------------------------------------------------------
# 3. restrict_web_to_ips
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRestrictWebToIps:
    def test_restricts_80_and_443(self):
        """80 and 443 rules are replaced with the given IP list."""
        client = MagicMock()
        client.get_firewall.return_value = _make_public_firewall()
        client.update_firewall.return_value = {"id": "fw-abc"}

        restrict_web_to_ips(client, "fw-abc", ["203.0.113.1"])

        kwargs = client.update_firewall.call_args[1]
        inbound = kwargs["inbound_rules"]
        web_rules = [r for r in inbound if r["ports"] in ("80", "443")]
        for rule in web_rules:
            assert rule["sources"]["addresses"] == ["203.0.113.1/32"]

    def test_ssh_stays_open(self):
        """SSH rule is preserved open-to-all."""
        client = MagicMock()
        client.get_firewall.return_value = _make_public_firewall()
        client.update_firewall.return_value = {"id": "fw-abc"}

        restrict_web_to_ips(client, "fw-abc", ["1.2.3.4"])

        kwargs = client.update_firewall.call_args[1]
        ssh_rules = [r for r in kwargs["inbound_rules"] if r["ports"] == "22"]
        assert len(ssh_rules) == 1
        assert "0.0.0.0/0" in ssh_rules[0]["sources"]["addresses"]

    def test_empty_ip_list_raises(self):
        """Empty ip_cidrs list raises CloudError D021."""
        client = MagicMock()
        with pytest.raises(CloudError) as exc_info:
            restrict_web_to_ips(client, "fw-abc", [])
        assert exc_info.value.error_code == "DANGO-D021"

    def test_preserves_outbound_rules(self):
        """Existing outbound rules are preserved unchanged."""
        client = MagicMock()
        fw = _make_public_firewall()
        client.get_firewall.return_value = fw
        client.update_firewall.return_value = {"id": "fw-abc"}

        restrict_web_to_ips(client, "fw-abc", ["1.2.3.4"])

        kwargs = client.update_firewall.call_args[1]
        assert kwargs["outbound_rules"] == DEFAULT_OUTBOUND_RULES

    def test_preserves_droplet_associations(self):
        """Droplet IDs are passed through to update_firewall."""
        client = MagicMock()
        fw = _make_public_firewall(droplet_ids=[10, 20])
        client.get_firewall.return_value = fw
        client.update_firewall.return_value = {"id": "fw-abc"}

        restrict_web_to_ips(client, "fw-abc", ["1.2.3.4"])

        kwargs = client.update_firewall.call_args[1]
        assert kwargs["droplet_ids"] == [10, 20]

    def test_multiple_ips(self):
        """Multiple IPs are all included in the rule sources."""
        client = MagicMock()
        client.get_firewall.return_value = _make_public_firewall()
        client.update_firewall.return_value = {"id": "fw-abc"}

        restrict_web_to_ips(client, "fw-abc", ["1.1.1.1", "2.2.2.2"])

        kwargs = client.update_firewall.call_args[1]
        web_rules = [r for r in kwargs["inbound_rules"] if r["ports"] in ("80", "443")]
        for rule in web_rules:
            addrs = rule["sources"]["addresses"]
            assert "1.1.1.1/32" in addrs
            assert "2.2.2.2/32" in addrs


# ---------------------------------------------------------------------------
# 4. allow_all_web
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAllowAllWeb:
    def test_reverts_inbound_to_public(self):
        """80/443 rules are changed back to 0.0.0.0/0."""
        client = MagicMock()
        client.get_firewall.return_value = _make_allowlist_firewall(["1.2.3.4/32"])
        client.update_firewall.return_value = {"id": "fw-abc"}

        allow_all_web(client, "fw-abc")

        kwargs = client.update_firewall.call_args[1]
        web_rules = [r for r in kwargs["inbound_rules"] if r["ports"] in ("80", "443")]
        for rule in web_rules:
            assert "0.0.0.0/0" in rule["sources"]["addresses"]

    def test_ssh_remains_open(self):
        """SSH rule is left unchanged."""
        client = MagicMock()
        client.get_firewall.return_value = _make_allowlist_firewall(["1.2.3.4/32"])
        client.update_firewall.return_value = {"id": "fw-abc"}

        allow_all_web(client, "fw-abc")

        kwargs = client.update_firewall.call_args[1]
        ssh_rules = [r for r in kwargs["inbound_rules"] if r["ports"] == "22"]
        assert len(ssh_rules) == 1

    def test_preserves_outbound_rules(self):
        """Outbound rules are not changed."""
        client = MagicMock()
        client.get_firewall.return_value = _make_allowlist_firewall(["1.2.3.4/32"])
        client.update_firewall.return_value = {"id": "fw-abc"}

        allow_all_web(client, "fw-abc")

        kwargs = client.update_firewall.call_args[1]
        assert kwargs["outbound_rules"] == DEFAULT_OUTBOUND_RULES


# ---------------------------------------------------------------------------
# 5. add_allowed_ip
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAddAllowedIp:
    def test_public_to_allowlist_mode(self):
        """Switches from public to allowlist with only the new IP."""
        client = MagicMock()
        client.get_firewall.return_value = _make_public_firewall()
        client.update_firewall.return_value = {"id": "fw-abc"}

        add_allowed_ip(client, "fw-abc", "203.0.113.1")

        kwargs = client.update_firewall.call_args[1]
        web_rules = [r for r in kwargs["inbound_rules"] if r["ports"] in ("80", "443")]
        for rule in web_rules:
            addrs = rule["sources"]["addresses"]
            assert "203.0.113.1/32" in addrs
            assert "0.0.0.0/0" not in addrs

    def test_append_to_existing_allowlist(self):
        """New IP appended when already in allowlist mode."""
        client = MagicMock()
        client.get_firewall.return_value = _make_allowlist_firewall(["1.1.1.1/32"])
        client.update_firewall.return_value = {"id": "fw-abc"}

        add_allowed_ip(client, "fw-abc", "2.2.2.2")

        kwargs = client.update_firewall.call_args[1]
        web_rules = [r for r in kwargs["inbound_rules"] if r["ports"] in ("80", "443")]
        for rule in web_rules:
            addrs = rule["sources"]["addresses"]
            assert "1.1.1.1/32" in addrs
            assert "2.2.2.2/32" in addrs

    def test_deduplication(self):
        """Duplicate IPs are deduplicated."""
        client = MagicMock()
        client.get_firewall.return_value = _make_allowlist_firewall(["1.1.1.1/32"])
        client.update_firewall.return_value = {"id": "fw-abc"}

        add_allowed_ip(client, "fw-abc", "1.1.1.1")

        kwargs = client.update_firewall.call_args[1]
        web_rules = [r for r in kwargs["inbound_rules"] if r["ports"] in ("80", "443")]
        for rule in web_rules:
            addrs = rule["sources"]["addresses"]
            assert addrs.count("1.1.1.1/32") == 1

    def test_invalid_ip_raises_before_api_call(self):
        """CloudError D020 raised without making an API call for bad IP."""
        client = MagicMock()
        with pytest.raises(CloudError) as exc_info:
            add_allowed_ip(client, "fw-abc", "not-an-ip")
        assert exc_info.value.error_code == "DANGO-D020"
        client.get_firewall.assert_not_called()

    def test_cidr_accepted(self):
        """CIDR notation is accepted and normalised."""
        client = MagicMock()
        client.get_firewall.return_value = _make_public_firewall()
        client.update_firewall.return_value = {"id": "fw-abc"}

        add_allowed_ip(client, "fw-abc", "10.0.0.0/24")

        kwargs = client.update_firewall.call_args[1]
        web_rules = [r for r in kwargs["inbound_rules"] if r["ports"] in ("80", "443")]
        for rule in web_rules:
            assert "10.0.0.0/24" in rule["sources"]["addresses"]


# ---------------------------------------------------------------------------
# 6. format_firewall_rules
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFormatFirewallRules:
    def test_inbound_and_outbound_rows(self):
        """Returns rows for both inbound and outbound rules."""
        fw = _make_public_firewall()
        rows = format_firewall_rules(fw)
        directions = {r["direction"] for r in rows}
        assert "inbound" in directions
        assert "outbound" in directions

    def test_inbound_row_fields(self):
        """Inbound rows have the correct field keys."""
        fw = _make_public_firewall()
        rows = format_firewall_rules(fw)
        inbound_rows = [r for r in rows if r["direction"] == "inbound"]
        assert len(inbound_rows) > 0
        for row in inbound_rows:
            assert "direction" in row
            assert "protocol" in row
            assert "ports" in row
            assert "sources_or_destinations" in row

    def test_empty_rules(self):
        """Empty firewall returns empty list."""
        fw: dict = {"inbound_rules": [], "outbound_rules": []}
        assert format_firewall_rules(fw) == []

    def test_sources_joined(self):
        """Multiple source addresses are comma-joined."""
        fw = _make_public_firewall()
        rows = format_firewall_rules(fw)
        inbound_rows = [r for r in rows if r["direction"] == "inbound"]
        ssh_row = next(r for r in inbound_rows if r["ports"] == "22")
        assert "0.0.0.0/0" in ssh_row["sources_or_destinations"]
        assert "::/0" in ssh_row["sources_or_destinations"]


# ---------------------------------------------------------------------------
# 7. save_firewall_metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSaveFirewallMetadata:
    def test_saves_firewall_id(self, tmp_path: Path):
        """Persists firewall_id to cloud.yml."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()

        save_firewall_metadata(tmp_path, "fw-uuid-123")

        from dango.config.loader import ConfigLoader

        loader = ConfigLoader(tmp_path)
        saved = loader.load_cloud_config()
        assert saved is not None
        assert saved.firewall_id == "fw-uuid-123"

    def test_updates_existing_config(self, tmp_path: Path):
        """Updates firewall_id while preserving other CloudConfig fields."""
        from dango.config.loader import ConfigLoader
        from dango.config.models import CloudConfig

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        loader = ConfigLoader(tmp_path)
        loader.save_cloud_config(CloudConfig(droplet_id=42, droplet_ip="1.2.3.4", region="nyc1"))

        save_firewall_metadata(tmp_path, "fw-new-id")

        saved = loader.load_cloud_config()
        assert saved is not None
        assert saved.firewall_id == "fw-new-id"
        assert saved.droplet_id == 42  # preserved
        assert saved.droplet_ip == "1.2.3.4"  # preserved
