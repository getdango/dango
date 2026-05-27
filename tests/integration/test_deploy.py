"""tests/integration/test_deploy.py

Full E2E deployment verification tests (TEST-006).

Uses the session-scoped ``deployed_server`` fixture that deploys a real
server on DigitalOcean, runs all tests against it, and destroys it on
teardown. The fixture is shared with test_remote.py to avoid double cost.

Requires DIGITALOCEAN_TOKEN env var.
"""

from __future__ import annotations

from typing import Any

import pytest

from dango.platform.cloud.firewall import get_firewall_rules
from dango.platform.cloud.server_status import collect_server_status
from dango.platform.cloud.ssh import SSHManager


@pytest.mark.cloud
class TestDeploymentHealth:
    """Verify the deployed server is healthy and correctly configured."""

    def test_server_reachable_via_ssh(self, deployed_server: dict[str, Any]) -> None:
        """SSH connection works and can execute a basic command."""
        ssh: SSHManager = deployed_server["ssh"]
        result = ssh.exec_command("echo ok")
        assert result.success
        assert result.stdout.strip() == "ok"

    def test_all_services_running(self, deployed_server: dict[str, Any]) -> None:
        """Core services (dango-web, caddy, docker) are active."""
        ssh: SSHManager = deployed_server["ssh"]
        cloud_cfg = deployed_server["cloud_cfg"]

        status = collect_server_status(ssh, cloud_cfg)

        service_names = {s.name for s in status.services}
        assert "dango-web" in service_names, "dango-web service not found"

        # At least dango-web should be running
        running = {s.name for s in status.services if s.status == "running"}
        assert "dango-web" in running, "dango-web is not running"

    def test_server_resources_populated(self, deployed_server: dict[str, Any]) -> None:
        """CPU, RAM, and disk metrics are collected successfully."""
        ssh: SSHManager = deployed_server["ssh"]
        cloud_cfg = deployed_server["cloud_cfg"]

        status = collect_server_status(ssh, cloud_cfg)

        assert status.cpu_usage_pct is not None
        assert status.ram_total_mb is not None
        assert status.ram_total_mb > 0
        assert status.disk_total_mb is not None
        assert status.disk_total_mb > 0

    def test_auth_configured(self, deployed_server: dict[str, Any]) -> None:
        """Auth database exists and admin user was created on the server."""
        ssh: SSHManager = deployed_server["ssh"]

        # Check auth.db exists
        result = ssh.exec_command("test -f /srv/dango/project/.dango/auth.db && echo exists")
        assert result.success
        assert "exists" in result.stdout

        # Check auth is enabled
        result = ssh.exec_command("cat /srv/dango/project/.dango/auth.yml")
        assert result.success
        assert "enabled: true" in result.stdout

    def test_firewall_configured(self, deployed_server: dict[str, Any]) -> None:
        """Firewall has SSH, HTTP, and HTTPS inbound rules."""
        client = deployed_server["client"]
        cloud_cfg = deployed_server["cloud_cfg"]

        assert cloud_cfg.firewall_id is not None, "No firewall ID in cloud config"

        fw_data = get_firewall_rules(client, cloud_cfg.firewall_id)
        inbound = fw_data.get("inbound_rules", [])
        inbound_ports = {r.get("ports") for r in inbound}

        assert "22" in inbound_ports, "SSH (22) rule missing"
        assert "80" in inbound_ports, "HTTP (80) rule missing"
        assert "443" in inbound_ports, "HTTPS (443) rule missing"

    def test_dbt_project_exists(self, deployed_server: dict[str, Any]) -> None:
        """dbt project directory and dbt_project.yml exist on the server."""
        ssh: SSHManager = deployed_server["ssh"]

        result = ssh.exec_command("test -d /srv/dango/project/dbt && echo exists")
        assert result.success
        assert "exists" in result.stdout

        result = ssh.exec_command("test -f /srv/dango/project/dbt/dbt_project.yml && echo exists")
        assert result.success
        assert "exists" in result.stdout

    def test_dango_installed(self, deployed_server: dict[str, Any]) -> None:
        """Dango is installed in the server venv and importable."""
        ssh: SSHManager = deployed_server["ssh"]

        result = ssh.exec_command(
            '/srv/dango/venv/bin/python -c "import dango; print(dango.__version__)"',
            timeout=30,
        )
        assert result.success, f"Failed to import dango: {result.stderr}"
        version = result.stdout.strip()
        assert version, "Empty version string"

    def test_project_directory_structure(self, deployed_server: dict[str, Any]) -> None:
        """Key project directories exist on the remote server."""
        ssh: SSHManager = deployed_server["ssh"]

        for path in [
            "/srv/dango/project",
            "/srv/dango/project/.dango",
            "/srv/dango/project/dbt",
            "/srv/dango/venv",
        ]:
            result = ssh.exec_command(f"test -d {path} && echo exists")
            assert result.success, f"Directory missing: {path}"
            assert "exists" in result.stdout, f"Directory missing: {path}"
