"""tests/unit/test_domain.py

Unit tests for DNS check and domain management
(dango/platform/cloud/domain.py).
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cloud_config(
    *,
    droplet_id: int = 123,
    droplet_ip: str = "203.0.113.42",
    domain: str | None = None,
):
    """Create a mock CloudConfig with the given attributes."""
    cfg = MagicMock()
    cfg.droplet_id = droplet_id
    cfg.droplet_ip = droplet_ip
    cfg.domain = domain
    cfg.ssh_key_path = ".dango/cloud_key"
    return cfg


def _make_ssh_mock(*, cat_stdout: str = "", cat_success: bool = False):
    """Create a mock SSHManager for domain tests."""
    from dango.platform.cloud.ssh import CommandResult

    ssh = MagicMock()
    # cat command to check existing Caddyfile
    cat_result = CommandResult(
        stdout=cat_stdout,
        stderr="" if cat_success else "No such file",
        exit_code=0 if cat_success else 1,
    )
    # reload command always succeeds
    reload_result = CommandResult(stdout="", stderr="", exit_code=0)

    def _exec_side_effect(command, timeout=None):
        if command.startswith("cat "):
            return cat_result
        return reload_result

    ssh.exec_command.side_effect = _exec_side_effect
    ssh.write_remote_file = MagicMock()
    return ssh


# ---------------------------------------------------------------------------
# 1. check_dns
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckDns:
    @patch("dango.platform.cloud.domain.socket.getaddrinfo")
    def test_dns_matches(self, mock_getaddr):
        """Returns (True, message) when domain resolves to expected IP."""
        from dango.platform.cloud.domain import check_dns

        mock_getaddr.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.42", 0)),
        ]
        ok, msg = check_dns("app.example.com", "203.0.113.42")
        assert ok is True
        assert "203.0.113.42" in msg

    @patch("dango.platform.cloud.domain.socket.getaddrinfo")
    def test_dns_mismatch(self, mock_getaddr):
        """Returns (False, message) when domain resolves to wrong IP."""
        from dango.platform.cloud.domain import check_dns

        mock_getaddr.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.51.100.1", 0)),
        ]
        ok, msg = check_dns("app.example.com", "203.0.113.42")
        assert ok is False
        assert "expected 203.0.113.42" in msg

    @patch("dango.platform.cloud.domain.socket.getaddrinfo")
    def test_dns_lookup_failure(self, mock_getaddr):
        """Returns (False, message) when DNS lookup fails."""
        from dango.platform.cloud.domain import check_dns

        mock_getaddr.side_effect = socket.gaierror("Name or service not known")
        ok, msg = check_dns("nonexistent.example.com", "203.0.113.42")
        assert ok is False
        assert "DNS lookup failed" in msg

    @patch("dango.platform.cloud.domain.socket.getaddrinfo")
    def test_dns_multiple_ips_one_matches(self, mock_getaddr):
        """Returns True when at least one resolved IP matches."""
        from dango.platform.cloud.domain import check_dns

        mock_getaddr.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.51.100.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.42", 0)),
        ]
        ok, msg = check_dns("app.example.com", "203.0.113.42")
        assert ok is True


# ---------------------------------------------------------------------------
# 2. set_domain
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSetDomain:
    @patch("dango.platform.cloud.domain.check_dns")
    @patch("dango.config.loader.ConfigLoader")
    def test_sets_domain_and_saves(self, mock_loader_cls, mock_check_dns, tmp_path):
        """set_domain writes HTTPS Caddyfile and saves domain to cloud.yml."""
        from dango.platform.cloud.domain import set_domain

        cloud_cfg = _make_cloud_config()
        mock_loader = MagicMock()
        mock_loader.load_cloud_config.return_value = cloud_cfg
        mock_loader_cls.return_value = mock_loader
        mock_check_dns.return_value = (True, "DNS OK")

        ssh = _make_ssh_mock()
        result = set_domain(ssh, tmp_path, "app.example.com")

        assert result["domain"] == "app.example.com"
        assert result["dns_ok"] is True
        assert result["caddyfile_updated"] is True
        assert cloud_cfg.domain == "app.example.com"
        mock_loader.save_cloud_config.assert_called_once_with(cloud_cfg)

    @patch("dango.platform.cloud.domain.check_dns")
    @patch("dango.config.loader.ConfigLoader")
    def test_dns_warning_non_blocking(self, mock_loader_cls, mock_check_dns, tmp_path):
        """DNS mismatch produces a warning but doesn't block the operation."""
        from dango.platform.cloud.domain import set_domain

        cloud_cfg = _make_cloud_config()
        mock_loader = MagicMock()
        mock_loader.load_cloud_config.return_value = cloud_cfg
        mock_loader_cls.return_value = mock_loader
        mock_check_dns.return_value = (False, "DNS not propagated")

        ssh = _make_ssh_mock()
        result = set_domain(ssh, tmp_path, "app.example.com")

        assert result["dns_ok"] is False
        assert result["dns_message"] == "DNS not propagated"
        # Should still succeed
        assert result["domain"] == "app.example.com"

    @patch("dango.config.loader.ConfigLoader")
    def test_no_droplet_ip_raises(self, mock_loader_cls, tmp_path):
        """Raises CloudProvisioningError when droplet_ip is missing."""
        from dango.platform.cloud.domain import set_domain

        cloud_cfg = _make_cloud_config(droplet_ip=None)
        mock_loader = MagicMock()
        mock_loader.load_cloud_config.return_value = cloud_cfg
        mock_loader_cls.return_value = mock_loader

        ssh = _make_ssh_mock()
        with pytest.raises(CloudProvisioningError, match="No droplet IP"):
            set_domain(ssh, tmp_path, "app.example.com")

    @patch("dango.platform.cloud.domain.check_dns")
    @patch("dango.config.loader.ConfigLoader")
    def test_caddyfile_unchanged_skips_write(self, mock_loader_cls, mock_check_dns, tmp_path):
        """If the Caddyfile already has correct content, skip the write."""
        from dango.platform.cloud._server_templates import build_caddyfile
        from dango.platform.cloud.domain import set_domain

        cloud_cfg = _make_cloud_config()
        mock_loader = MagicMock()
        mock_loader.load_cloud_config.return_value = cloud_cfg
        mock_loader_cls.return_value = mock_loader
        mock_check_dns.return_value = (True, "DNS OK")

        expected_content = build_caddyfile("app.example.com")
        ssh = _make_ssh_mock(cat_stdout=expected_content, cat_success=True)
        result = set_domain(ssh, tmp_path, "app.example.com")

        assert result["caddyfile_updated"] is False
        ssh.write_remote_file.assert_not_called()


# ---------------------------------------------------------------------------
# 3. remove_domain
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoveDomain:
    @patch("dango.config.loader.ConfigLoader")
    def test_removes_domain_and_saves(self, mock_loader_cls, tmp_path):
        """remove_domain writes HTTP-only Caddyfile and clears domain."""
        from dango.platform.cloud.domain import remove_domain

        cloud_cfg = _make_cloud_config(domain="app.example.com")
        mock_loader = MagicMock()
        mock_loader.load_cloud_config.return_value = cloud_cfg
        mock_loader_cls.return_value = mock_loader

        ssh = _make_ssh_mock()
        result = remove_domain(ssh, tmp_path)

        assert result["previous_domain"] == "app.example.com"
        assert result["caddyfile_updated"] is True
        assert cloud_cfg.domain is None
        mock_loader.save_cloud_config.assert_called_once()

    @patch("dango.config.loader.ConfigLoader")
    def test_no_previous_domain(self, mock_loader_cls, tmp_path):
        """remove_domain works even when no domain was set."""
        from dango.platform.cloud.domain import remove_domain

        cloud_cfg = _make_cloud_config(domain=None)
        mock_loader = MagicMock()
        mock_loader.load_cloud_config.return_value = cloud_cfg
        mock_loader_cls.return_value = mock_loader

        ssh = _make_ssh_mock()
        result = remove_domain(ssh, tmp_path)

        assert result["previous_domain"] is None

    @patch("dango.config.loader.ConfigLoader")
    def test_no_cloud_config_raises(self, mock_loader_cls, tmp_path):
        """Raises CloudProvisioningError when cloud.yml doesn't exist."""
        from dango.platform.cloud.domain import remove_domain

        mock_loader = MagicMock()
        mock_loader.load_cloud_config.return_value = None
        mock_loader_cls.return_value = mock_loader

        ssh = _make_ssh_mock()
        with pytest.raises(CloudProvisioningError, match="No cloud configuration"):
            remove_domain(ssh, tmp_path)
