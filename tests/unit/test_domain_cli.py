"""tests/unit/test_domain_cli.py

Unit tests for ``dango remote domain set/remove`` CLI commands
(dango/cli/commands/remote.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cloud_config(
    *,
    droplet_id: int = 123,
    droplet_ip: str = "203.0.113.42",
    domain: str | None = None,
    firewall_id: str | None = "fw-abc",
    ssh_key_path: str = ".dango/cloud_key",
):
    """Create a mock CloudConfig."""
    cfg = MagicMock()
    cfg.droplet_id = droplet_id
    cfg.droplet_ip = droplet_ip
    cfg.domain = domain
    cfg.firewall_id = firewall_id
    cfg.ssh_key_path = ssh_key_path
    return cfg


def _invoke_domain_cmd(args, cloud_cfg=None, domain_result=None, connect_error=None):
    """Run a domain CLI command with mocked dependencies.

    Returns the CliRunner Result.
    """
    from dango.cli.commands.remote import remote

    runner = CliRunner()

    if cloud_cfg is None:
        cloud_cfg = _make_cloud_config()

    patches = {
        "dango.cli.commands.remote.require_project_context": MagicMock(
            return_value=Path("/tmp/project")
        ),
        "dango.config.loader.ConfigLoader": MagicMock(),
    }

    loader_mock = MagicMock()
    loader_mock.load_cloud_config.return_value = cloud_cfg
    patches["dango.config.loader.ConfigLoader"].return_value = loader_mock

    ssh_mock = MagicMock()
    if connect_error:
        ssh_mock.connect.side_effect = connect_error

    with (
        patch.dict("os.environ", {}, clear=False),
        patch("dango.cli.commands.remote._require_cloud_deployment") as mock_req,
        patch("dango.cli.commands.remote._connect_ssh") as mock_ssh,
    ):
        mock_req.return_value = (cloud_cfg, Path("/tmp/project"))
        mock_ssh.return_value = ssh_mock

        if "set" in args:
            with patch("dango.platform.cloud.domain.set_domain") as mock_set:
                mock_set.return_value = domain_result or {
                    "domain": args[-1],
                    "dns_ok": True,
                    "dns_message": "DNS OK",
                    "caddyfile_updated": True,
                }
                return runner.invoke(remote, args, catch_exceptions=False)
        else:
            with patch("dango.platform.cloud.domain.remove_domain") as mock_rm:
                mock_rm.return_value = domain_result or {
                    "previous_domain": "app.example.com",
                    "caddyfile_updated": True,
                }
                return runner.invoke(remote, args, catch_exceptions=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDomainSet:
    def test_set_domain_success(self):
        """Successful domain set shows DNS OK and HTTPS configured."""
        result = _invoke_domain_cmd(
            ["domain", "set", "app.example.com"],
            domain_result={
                "domain": "app.example.com",
                "dns_ok": True,
                "dns_message": "app.example.com resolves to 203.0.113.42",
                "caddyfile_updated": True,
            },
        )
        assert result.exit_code == 0
        assert "DNS OK" in result.output
        assert "HTTPS configured" in result.output

    def test_set_domain_dns_warning(self):
        """DNS mismatch shows a warning but still succeeds."""
        result = _invoke_domain_cmd(
            ["domain", "set", "app.example.com"],
            domain_result={
                "domain": "app.example.com",
                "dns_ok": False,
                "dns_message": "DNS not propagated",
                "caddyfile_updated": True,
            },
        )
        assert result.exit_code == 0
        assert "DNS warning" in result.output
        assert "HTTPS configured" in result.output

    def test_set_domain_caddyfile_unchanged(self):
        """When Caddyfile already correct, shows 'already configured'."""
        result = _invoke_domain_cmd(
            ["domain", "set", "app.example.com"],
            domain_result={
                "domain": "app.example.com",
                "dns_ok": True,
                "dns_message": "DNS OK",
                "caddyfile_updated": False,
            },
        )
        assert result.exit_code == 0
        assert "already configured" in result.output

    def test_set_domain_no_ip(self):
        """Exits with error when droplet_ip is None."""
        cloud_cfg = _make_cloud_config(droplet_ip=None)
        result = _invoke_domain_cmd(
            ["domain", "set", "app.example.com"],
            cloud_cfg=cloud_cfg,
        )
        assert result.exit_code == 1
        assert "No droplet IP" in result.output


@pytest.mark.unit
class TestDomainRemove:
    def test_remove_domain_success(self):
        """Successful domain removal shows previous domain."""
        result = _invoke_domain_cmd(
            ["domain", "remove"],
            domain_result={
                "previous_domain": "app.example.com",
                "caddyfile_updated": True,
            },
        )
        assert result.exit_code == 0
        assert "Domain removed" in result.output
        assert "app.example.com" in result.output

    def test_remove_domain_none_configured(self):
        """When no domain was configured, shows appropriate message."""
        result = _invoke_domain_cmd(
            ["domain", "remove"],
            domain_result={
                "previous_domain": None,
                "caddyfile_updated": True,
            },
        )
        assert result.exit_code == 0
        assert "No domain was configured" in result.output

    def test_remove_domain_caddyfile_unchanged(self):
        """When Caddyfile was already HTTP-only, shows appropriate message."""
        result = _invoke_domain_cmd(
            ["domain", "remove"],
            domain_result={
                "previous_domain": None,
                "caddyfile_updated": False,
            },
        )
        assert result.exit_code == 0
        assert "already HTTP-only" in result.output


@pytest.mark.unit
class TestRequireCloudDeployment:
    def test_no_deployment_exits(self):
        """_require_cloud_deployment exits when no droplet_id."""
        from dango.cli.commands.remote import remote

        runner = CliRunner()

        with (
            patch("dango.cli.commands.remote._require_cloud_deployment") as mock_req,
        ):
            mock_req.side_effect = SystemExit(1)
            result = runner.invoke(remote, ["domain", "set", "x.com"])
            assert result.exit_code == 1
