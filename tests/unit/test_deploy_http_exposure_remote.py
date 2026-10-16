"""tests/unit/test_deploy_http_exposure_remote.py

Web-port restriction at provisioning (explicit IPs, detect, health check), `remote domain set` reopening, status line.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

_PASSWORD = "Str0ng-and-l0ng-passw0rd!"


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "sources.yml").write_text("sources: []")
    (dango_dir / "project.yml").write_text("project:\n  name: test\n")
    return tmp_path


def _cmd_result(stdout: str = "", exit_code: int = 0) -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.stderr = ""
    result.exit_code = exit_code
    result.success = exit_code == 0
    return result


def _config(allowed_ips: list[str], domain: str | None = None):
    from dango.cli.commands.deploy_wizard import WizardConfig

    return WizardConfig(
        region="nyc1",
        size_slug="s-2vcpu-4gb",
        size_tier=None,
        domain=domain,
        admin_email="admin@example.com",
        admin_password=_PASSWORD,
        skip_oauth=True,
        enable_backups=False,
        monthly_cost=24,
        allowed_ips=allowed_ips,
    )


def _provision(project_root: Path, config, ssh_client: str):
    """Run run_provisioning with everything external faked.

    *ssh_client* is what ``echo $SSH_CLIENT`` prints on the server.
    Returns (result, restrict_mock, health_mock, ssh_mock).
    """
    from dango.cli.commands.deploy_provision import run_provisioning

    ssh = MagicMock()
    ssh.generate_key_pair.return_value = "ssh-ed25519 AAAA"

    def _exec(command: str, *args, **kwargs):
        if "SSH_CLIENT" in command:
            return _cmd_result(ssh_client + "\n")
        return _cmd_result()

    ssh.exec_command.side_effect = _exec
    client = MagicMock()
    client.upload_ssh_key.return_value = {"id": 111}
    restrict = MagicMock(return_value={"id": "fw-333"})
    health = MagicMock(return_value=True)
    with (
        patch("dango.platform.cloud.digitalocean.DigitalOceanClient", return_value=client),
        patch("dango.platform.cloud.ssh.SSHManager", return_value=ssh),
        patch(
            "dango.platform.cloud.provisioning.provision_droplet",
            return_value={
                "id": 222,
                "networks": {"v4": [{"type": "public", "ip_address": "1.2.3.4"}]},
            },
        ),
        patch(
            "dango.platform.cloud.firewall.create_default_firewall", return_value={"id": "fw-333"}
        ),
        patch("dango.platform.cloud.firewall.restrict_web_to_ips", restrict),
        patch(
            "dango.platform.cloud.server_setup.setup_server", return_value=MagicMock(warnings=[])
        ),
        patch("dango.platform.cloud.server_setup.resolve_install_source", return_value="getdango"),
        patch("dango.platform.cloud.file_sync.sync_project_files"),
        patch("dango.platform.cloud.provisioning.save_provisioning_metadata"),
        patch("dango.platform.cloud.backup.get_remote_compose_project_name", return_value="p"),
        patch("dango.cli.commands.deploy_provision._health_check", health),
        patch("dango.cli.commands.deploy_provision._wait_for_metabase", return_value=True),
        patch("dango.cli.commands.deploy_provision.time.sleep"),
    ):
        result = run_provisioning(project_root, config, non_interactive=True)
    return result, restrict, health, client


# ---------------------------------------------------------------------------
# Provisioning: restriction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProvisioningRestriction:
    def test_provisioning_restricts_web_to_allowed_ips(self, project_root):
        result, restrict, _health, client = _provision(
            project_root, _config(["198.51.100.7/32"]), "198.51.100.7 51234 22"
        )
        restrict.assert_called_once_with(client, "fw-333", ["198.51.100.7/32"])
        assert result.allowed_ips == ["198.51.100.7/32"]

    def test_provisioning_without_allowed_ips_leaves_firewall(self, project_root):
        result, restrict, _health, _client = _provision(
            project_root, _config([]), "198.51.100.7 51234 22"
        )
        restrict.assert_not_called()
        assert result.allowed_ips == []

    def test_wizard_detect_uses_ssh_client_ip(self, project_root, capsys):
        result, restrict, _health, client = _provision(
            project_root, _config(["detect"]), "203.0.113.7 51234 22"
        )
        restrict.assert_called_once_with(client, "fw-333", ["203.0.113.7/32"])
        assert result.allowed_ips == ["203.0.113.7/32"]
        out = " ".join(capsys.readouterr().out.split())
        assert "203.0.113.7/32" in out
        assert "dango remote firewall allow-ip" in out

    @pytest.mark.parametrize("ssh_client", ["", "2001:db8::1 51234 22", "garbage"])
    def test_wizard_detect_invalid_or_ipv6_leaves_firewall_and_prints_command(
        self, project_root, capsys, ssh_client
    ):
        result, restrict, _health, _client = _provision(
            project_root, _config(["detect"]), ssh_client
        )
        restrict.assert_not_called()
        assert result.allowed_ips == []
        out = " ".join(capsys.readouterr().out.split())
        assert "dango remote firewall allow-ip <your-ip>" in out


# ---------------------------------------------------------------------------
# Provisioning: client-side health check
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHealthCheckWithRestriction:
    def test_health_check_skipped_when_deployer_not_in_allowed_ips(self, project_root):
        result, _restrict, health, _client = _provision(
            project_root, _config(["198.51.100.0/24"]), "203.0.113.7 51234 22"
        )
        health.assert_not_called()
        joined = " ".join(result.warnings)
        assert "Health check skipped" in joined
        assert "198.51.100.0/24" in joined
        assert "Health check failed" not in joined

    def test_health_check_runs_when_deployer_in_allowed_ips(self, project_root):
        result, _restrict, health, _client = _provision(
            project_root, _config(["203.0.113.0/24"]), "203.0.113.7 51234 22"
        )
        health.assert_called_once()
        assert not any("skipped" in w for w in result.warnings)

    def test_health_check_runs_when_unrestricted(self, project_root):
        _result, _restrict, health, _client = _provision(
            project_root, _config([]), "203.0.113.7 51234 22"
        )
        health.assert_called_once()


# ---------------------------------------------------------------------------
# remote domain set
# ---------------------------------------------------------------------------


def _firewall(web_sources: list[str]) -> dict:
    return {
        "id": "fw-abc",
        "name": "dango-fw-123",
        "inbound_rules": [
            {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}},
            {"protocol": "tcp", "ports": "80", "sources": {"addresses": web_sources}},
            {"protocol": "tcp", "ports": "443", "sources": {"addresses": web_sources}},
        ],
        "outbound_rules": [],
        "droplet_ids": [123],
        "tags": [],
    }


def _domain_set(web_sources: list[str] | None, *, client_error: Exception | None = None):
    """Invoke `dango remote domain set` with a fake DO client; return (result, calls)."""
    from dango.cli.commands.remote import remote

    calls: list[str] = []
    cfg = MagicMock()
    cfg.droplet_ip = "203.0.113.42"
    cfg.domain = None
    cfg.firewall_id = "fw-abc"
    cfg.provider = "digitalocean"
    cfg.ssh_key_path = ".dango/cloud_key"

    client = MagicMock()
    client.get_firewall.return_value = _firewall(web_sources or [])

    def _update(**kwargs):
        calls.append("update_firewall")
        return {"id": "fw-abc", **kwargs}

    client.update_firewall.side_effect = _update

    def _set_domain(*args, **kwargs):
        calls.append("set_domain")
        return {
            "domain": "app.example.com",
            "dns_ok": True,
            "dns_message": "ok",
            "caddyfile_updated": True,
        }

    make_client = (
        MagicMock(side_effect=client_error) if client_error else MagicMock(return_value=client)
    )
    with (
        patch(
            "dango.cli.commands.remote._require_cloud_deployment",
            return_value=(cfg, Path("/tmp/p")),
        ),
        patch("dango.cli.commands.remote._connect_ssh", return_value=MagicMock()),
        patch("dango.cli.commands.remote._make_client", make_client),
        patch("dango.platform.cloud.domain.set_domain", side_effect=_set_domain),
    ):
        result = CliRunner().invoke(
            remote, ["domain", "set", "app.example.com"], catch_exceptions=False
        )
    return result, calls, client


@pytest.mark.unit
class TestDomainSetReopensWebPorts:
    def test_domain_set_reopens_web_ports_before_writing_caddyfile(self):
        result, calls, client = _domain_set(["203.0.113.7/32"])
        assert result.exit_code == 0, result.output
        assert calls == ["update_firewall", "set_domain"]
        rules = client.update_firewall.call_args.kwargs["inbound_rules"]
        web = [r for r in rules if r["ports"] in ("80", "443")]
        assert all("0.0.0.0/0" in r["sources"]["addresses"] for r in web)
        assert "reopened" in result.output

    def test_domain_set_public_firewall_untouched(self):
        result, calls, _client = _domain_set(["0.0.0.0/0", "::/0"])
        assert result.exit_code == 0, result.output
        assert calls == ["set_domain"]

    def test_domain_set_without_token_prints_command_and_continues(self):
        from dango.exceptions import CloudAuthError

        result, calls, _client = _domain_set(
            None, client_error=CloudAuthError("No DigitalOcean token found.")
        )
        assert result.exit_code == 0, result.output
        assert calls == ["set_domain"]
        out = " ".join(result.output.split())
        assert "dango remote firewall allow-all" in out
        assert "certificate" in out


# ---------------------------------------------------------------------------
# remote status
# ---------------------------------------------------------------------------


def _status(tmp_path: Path, domain: str | None) -> str:
    from dango.cli.commands.remote import remote
    from dango.platform.cloud.server_status import ServerStatus

    cfg = MagicMock()
    cfg.droplet_ip = "1.2.3.4"
    cfg.droplet_id = 42
    cfg.firewall_id = "fw-abc"
    cfg.ssh_key_path = ".dango/cloud_key"
    cfg.region = "nyc1"
    cfg.size = "s-2vcpu-4gb"
    cfg.domain = domain
    cfg.provider = "digitalocean"
    loader = MagicMock()
    loader.load_cloud_config.return_value = cfg
    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch("dango.config.loader.ConfigLoader", return_value=loader),
        patch("dango.platform.cloud.ssh.SSHManager", return_value=MagicMock()),
        patch(
            "dango.platform.cloud.server_status.collect_server_status",
            return_value=ServerStatus(dango_version="1.0.14"),
        ),
        patch("dango.platform.cloud.server_status.check_latest_pypi_version", return_value=None),
    ):
        result = CliRunner().invoke(
            remote, ["status"], obj={"project_root": tmp_path}, catch_exceptions=False
        )
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())


@pytest.mark.unit
class TestStatusHttpOnly:
    def test_status_shows_http_only(self, tmp_path):
        assert "HTTP only (unencrypted)" in _status(tmp_path, None)

    def test_status_with_domain_has_no_http_line(self, tmp_path):
        assert "HTTP only" not in _status(tmp_path, "app.example.com")
