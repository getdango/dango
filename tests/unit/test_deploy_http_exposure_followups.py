"""tests/unit/test_deploy_http_exposure_followups.py

Review follow-ups for the HTTP-exposure change: guidance with a domain, the
firewall warning, the domain-removal warning and read-failure handling.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.ssh import CommandResult


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-9;]*m", "", text))


def _cfg(domain: str | None) -> MagicMock:
    cfg = MagicMock()
    cfg.droplet_ip = "203.0.113.42"
    cfg.domain = domain
    cfg.firewall_id = "fw-abc"
    cfg.provider = "digitalocean"
    cfg.ssh_key_path = ".dango/cloud_key"
    return cfg


@pytest.mark.unit
class TestDomainGuidanceKeepsWebPortsOpen:
    def test_domain_with_allow_ip_error_does_not_suggest_restricting(self):
        from dango.cli.commands.deploy_wizard import resolve_exposure_flags

        with pytest.raises(click.UsageError) as exc:
            resolve_exposure_flags(
                domain="app.example.com",
                allow_ip=["203.0.113.7"],
                accept_http_exposure=False,
                byos=False,
                require=True,
            )
        msg = _plain(str(exc.value.message))
        assert "then restrict access" not in msg
        assert "firewall allow-ip" not in msg
        assert "renew" in msg

    def test_docs_do_not_tell_users_to_restrict_after_domain_set(self):
        text = Path("docs/guides/deploy-any-server.md").read_text()
        assert "restrict them again afterwards" not in text
        assert "renew" in text


def _allow_ip(domain: str | None):
    from dango.cli.commands.remote import remote

    with (
        patch(
            "dango.cli.commands.remote._load_cloud_config_or_fail",
            return_value=(_cfg(domain), "fw-abc"),
        ),
        patch("dango.cli.commands.remote._make_client", return_value=MagicMock()),
        patch("dango.platform.cloud.firewall.add_allowed_ip", return_value={"name": "fw"}),
    ):
        return CliRunner().invoke(
            remote, ["firewall", "allow-ip", "203.0.113.7"], catch_exceptions=False
        )


@pytest.mark.unit
class TestAllowIpWarnsWithDomain:
    def test_warns_about_certificate_renewal_when_domain_configured(self):
        result = _allow_ip("app.example.com")
        assert result.exit_code == 0, result.output
        out = _plain(result.output)
        assert "renew" in out
        assert "app.example.com" in out

    def test_no_warning_without_domain(self):
        result = _allow_ip(None)
        assert result.exit_code == 0, result.output
        assert "renew" not in _plain(result.output)


def _domain_remove(previous: str | None):
    from dango.cli.commands.remote import remote

    with (
        patch(
            "dango.cli.commands.remote._require_cloud_deployment",
            return_value=(_cfg(previous), Path("/tmp/p")),
        ),
        patch("dango.cli.commands.remote._connect_ssh", return_value=MagicMock()),
        patch(
            "dango.platform.cloud.domain.remove_domain",
            return_value={"previous_domain": previous, "caddyfile_updated": True},
        ),
    ):
        return CliRunner().invoke(remote, ["domain", "remove"], catch_exceptions=False)


@pytest.mark.unit
class TestDomainRemoveWarns:
    def test_prints_http_exposure_warning(self):
        result = _domain_remove("app.example.com")
        assert result.exit_code == 0, result.output
        out = _plain(result.output)
        assert "unencrypted" in out
        assert "dango remote firewall allow-ip" in out
        assert "dango remote domain set" in out


def _ssh(exit_code: int, stdout: str = "", stderr: str = "") -> MagicMock:
    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
    return ssh


@pytest.mark.unit
class TestReadProjectFileDistinguishesFailure:
    def test_present_file_returns_content(self):
        from dango.platform.cloud.project_files import read_project_file

        assert read_project_file(_ssh(0, "A=1\n"), "/srv/x/.env") == "A=1\n"

    def test_absent_file_returns_empty(self):
        from dango.platform.cloud.project_files import read_project_file

        # The remote script exits 3 when the path does not exist
        assert read_project_file(_ssh(3), "/srv/x/.env") == ""

    def test_read_failure_raises(self):
        from dango.platform.cloud.project_files import read_project_file

        with pytest.raises(CloudProvisioningError, match="Permission denied"):
            read_project_file(_ssh(1, "", "cat: Permission denied"), "/srv/x/.env")

    def test_absent_is_decided_by_the_remote_script(self):
        from dango.platform.cloud.project_files import read_project_file

        ssh = _ssh(0, "")
        read_project_file(ssh, "/srv/x/.env")
        command = ssh.exec_command.call_args.args[0]
        assert "|| true" not in command
        assert "2>/dev/null" not in command
