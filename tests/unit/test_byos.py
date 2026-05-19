"""tests/unit/test_byos.py

Unit tests for BYOS (Bring Your Own Server) deployment path.

Tests cover:
- BYOSConfig dataclass
- BYOS wizard validation (SSH connectivity, non-interactive)
- UFW setup step in server_setup.py
- BYOS guards on DO-only operations (resize, migrate, firewall)
- CloudConfig provider field
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. CloudConfig provider field
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCloudConfigProvider:
    def test_default_provider_is_digitalocean(self):
        """Default provider should be 'digitalocean'."""
        from dango.config.models import CloudConfig

        config = CloudConfig()
        assert config.provider == "digitalocean"

    def test_byos_provider(self):
        """Provider can be set to 'byos'."""
        from dango.config.models import CloudConfig

        config = CloudConfig(provider="byos", droplet_ip="1.2.3.4")
        assert config.provider == "byos"
        assert config.droplet_ip == "1.2.3.4"
        assert config.droplet_id is None

    def test_backwards_compatible_no_provider(self):
        """Existing cloud.yml without provider field defaults to digitalocean."""
        from dango.config.models import CloudConfig

        config = CloudConfig(droplet_id=123, droplet_ip="1.2.3.4")
        assert config.provider == "digitalocean"


# ---------------------------------------------------------------------------
# 2. BYOSConfig dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBYOSConfig:
    def test_byos_config_creation(self):
        """BYOSConfig can be created with required fields."""
        from dango.cli.commands.deploy_wizard import BYOSConfig

        config = BYOSConfig(
            server_ip="1.2.3.4",
            ssh_user="root",
            ssh_key_path="/home/user/.ssh/id_ed25519",
            domain="example.com",
            admin_email="admin@example.com",
            admin_password="SecurePassword123!",
            skip_oauth=False,
        )
        assert config.server_ip == "1.2.3.4"
        assert config.ssh_user == "root"
        assert config.domain == "example.com"

    def test_byos_config_no_domain(self):
        """BYOSConfig works without a domain."""
        from dango.cli.commands.deploy_wizard import BYOSConfig

        config = BYOSConfig(
            server_ip="1.2.3.4",
            ssh_user="root",
            ssh_key_path="/path/to/key",
            domain=None,
            admin_email="admin@example.com",
            admin_password="SecurePassword123!",
            skip_oauth=True,
        )
        assert config.domain is None
        assert config.skip_oauth is True


# ---------------------------------------------------------------------------
# 3. BYOSResult dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBYOSResult:
    def test_byos_result_creation(self):
        """BYOSResult can be created with required fields."""
        from dango.cli.commands.deploy_provision import BYOSResult

        result = BYOSResult(server_ip="1.2.3.4")
        assert result.server_ip == "1.2.3.4"
        assert result.domain is None
        assert result.url == ""
        assert result.warnings == []

    def test_byos_result_with_warnings(self):
        """BYOSResult can carry warnings."""
        from dango.cli.commands.deploy_provision import BYOSResult

        result = BYOSResult(
            server_ip="1.2.3.4",
            domain="example.com",
            url="https://example.com",
            warnings=["Health check failed"],
        )
        assert len(result.warnings) == 1


# ---------------------------------------------------------------------------
# 4. BYOS non-interactive validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBYOSNonInteractive:
    def test_missing_server_ip_exits(self, tmp_path):
        """--byos without --server-ip should fail."""
        from dango.cli.commands.deploy_wizard import run_byos_non_interactive

        # Create sources.yml
        (tmp_path / ".dango").mkdir(parents=True)
        (tmp_path / ".dango" / "sources.yml").write_text("sources: []\n")

        with pytest.raises(SystemExit):
            run_byos_non_interactive(tmp_path, server_ip=None)

    def test_missing_admin_email_exits(self, tmp_path):
        """--byos without --admin-email should fail."""
        from dango.cli.commands.deploy_wizard import run_byos_non_interactive

        (tmp_path / ".dango").mkdir(parents=True)
        (tmp_path / ".dango" / "sources.yml").write_text("sources: []\n")

        with pytest.raises(SystemExit):
            run_byos_non_interactive(tmp_path, server_ip="1.2.3.4", admin_email=None)

    def test_missing_sources_yml_exits(self, tmp_path):
        """BYOS should fail without sources.yml."""
        from dango.cli.commands.deploy_wizard import run_byos_non_interactive

        (tmp_path / ".dango").mkdir(parents=True)
        # No sources.yml

        with pytest.raises(SystemExit):
            run_byos_non_interactive(
                tmp_path,
                server_ip="1.2.3.4",
                admin_email="a@b.com",
                admin_password="SecurePassword123!",
            )

    def test_missing_ssh_key_exits(self, tmp_path):
        """BYOS should fail if SSH key doesn't exist."""
        from dango.cli.commands.deploy_wizard import run_byos_non_interactive

        (tmp_path / ".dango").mkdir(parents=True)
        (tmp_path / ".dango" / "sources.yml").write_text("sources: []\n")

        with pytest.raises(SystemExit):
            run_byos_non_interactive(
                tmp_path,
                server_ip="1.2.3.4",
                ssh_key_path="/nonexistent/key",
                admin_email="a@b.com",
                admin_password="SecurePassword123!",
            )


# ---------------------------------------------------------------------------
# 5. SSH connectivity validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSSHValidation:
    def test_ssh_connection_failure_exits(self):
        """SSH connection failure should raise SystemExit."""
        from dango.cli.commands.deploy_wizard import _validate_ssh_connectivity

        with patch("dango.platform.cloud.ssh.SSHManager") as MockSSH:
            mock_ssh = MockSSH.return_value
            mock_ssh.connect.side_effect = ConnectionError("Connection refused")

            with pytest.raises(SystemExit):
                _validate_ssh_connectivity("1.2.3.4", "root", "/path/to/key")

    def test_non_ubuntu_warns(self):
        """Non-Ubuntu OS should warn but not fail."""
        from dango.cli.commands.deploy_wizard import _validate_ssh_connectivity
        from dango.platform.cloud.ssh import CommandResult

        with patch("dango.platform.cloud.ssh.SSHManager") as MockSSH:
            mock_ssh = MockSSH.return_value
            mock_ssh.connect.return_value = None
            mock_ssh.exec_command.return_value = CommandResult(
                stdout='ID=debian\nNAME="Debian"', stderr="", exit_code=0
            )
            mock_ssh.disconnect.return_value = None

            # Should not raise — just warns
            _validate_ssh_connectivity("1.2.3.4", "root", "/path/to/key")


# ---------------------------------------------------------------------------
# 6. UFW setup step
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUFWSetup:
    def _make_ssh_mock(self, *, ufw_active=False, rules_present=False):
        """Return a mock SSHManager for UFW testing."""
        from dango.platform.cloud.ssh import CommandResult

        def _exec(command, timeout=None):
            if "ufw status" in command and "grep -q" in command:
                return CommandResult(
                    stdout="",
                    stderr="",
                    exit_code=0 if ufw_active else 1,
                )
            if "ufw status | grep" in command:
                return CommandResult(
                    stdout="80/tcp ALLOW",
                    stderr="",
                    exit_code=0 if rules_present else 1,
                )
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh = MagicMock()
        ssh.exec_command.side_effect = _exec
        return ssh

    def test_ufw_installs_when_not_active(self):
        """UFW step should install and enable when not active."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_ufw

        ssh = self._make_ssh_mock(ufw_active=False)
        result = SetupResult()

        _setup_ufw(ssh, result, None)

        assert "ufw" in result.steps_completed
        assert "ufw" not in result.steps_skipped

    def test_ufw_skips_when_active_with_rules(self):
        """UFW step should skip when already active with rules."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_ufw

        ssh = self._make_ssh_mock(ufw_active=True, rules_present=True)
        result = SetupResult()

        _setup_ufw(ssh, result, None)

        assert "ufw" in result.steps_skipped
        assert "ufw" not in result.steps_completed

    def test_setup_server_with_ufw(self):
        """setup_server(setup_ufw=True) includes the UFW step."""
        from dango.platform.cloud.server_setup import setup_server
        from dango.platform.cloud.ssh import CommandResult

        def _exec(command, timeout=None):
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh = MagicMock()
        ssh.exec_command.side_effect = _exec
        ssh.write_remote_file = MagicMock()

        result = setup_server(ssh, setup_ufw=True)

        assert "ufw" in result.steps_completed or "ufw" in result.steps_skipped

    def test_setup_server_without_ufw(self):
        """setup_server(setup_ufw=False) skips the UFW step."""
        from dango.platform.cloud.server_setup import setup_server
        from dango.platform.cloud.ssh import CommandResult

        def _exec(command, timeout=None):
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh = MagicMock()
        ssh.exec_command.side_effect = _exec
        ssh.write_remote_file = MagicMock()

        result = setup_server(ssh, setup_ufw=False)

        assert "ufw" not in result.steps_completed
        assert "ufw" not in result.steps_skipped


# ---------------------------------------------------------------------------
# 7. BYOS guards on DO-only operations
# ---------------------------------------------------------------------------


_PATCH_REQUIRE = "dango.cli.commands.remote._require_cloud_deployment"
_PATCH_LOADER = "dango.config.loader.ConfigLoader"
_PATCH_REQUIRE_CTX = "dango.cli.utils.require_project_context"


def _make_byos_config() -> MagicMock:
    """Return a mock CloudConfig for BYOS."""
    cfg = MagicMock()
    cfg.provider = "byos"
    cfg.droplet_id = None
    cfg.droplet_ip = "1.2.3.4"
    cfg.firewall_id = None
    cfg.ssh_key_path = ".dango/cloud_key"
    cfg.region = "nyc1"
    cfg.size = "s-2vcpu-4gb"
    cfg.spaces = None
    return cfg


@pytest.mark.unit
class TestBYOSGuardResize:
    def test_byos_resize_blocked(self, tmp_path):
        """Resize should be blocked for BYOS deployments."""
        from click.testing import CliRunner

        from dango.cli.commands.remote import remote

        cfg = _make_byos_config()

        with (
            patch(_PATCH_REQUIRE, return_value=(cfg, tmp_path)),
            patch("dango.cli.utils.find_project_root", return_value=tmp_path),
        ):
            runner = CliRunner()
            result = runner.invoke(
                remote, ["resize", "s-4vcpu-8gb"], obj={"project_root": tmp_path}
            )

        assert result.exit_code == 1
        assert "not available for BYOS" in result.output


@pytest.mark.unit
class TestBYOSGuardMigrate:
    def test_byos_migrate_blocked(self, tmp_path):
        """Migrate should be blocked for BYOS deployments."""
        from click.testing import CliRunner

        from dango.cli.commands.remote import remote

        cfg = _make_byos_config()

        with (
            patch(_PATCH_REQUIRE, return_value=(cfg, tmp_path)),
            patch("dango.cli.utils.find_project_root", return_value=tmp_path),
        ):
            runner = CliRunner()
            result = runner.invoke(
                remote,
                ["migrate", "--size", "s-4vcpu-8gb"],
                obj={"project_root": tmp_path},
            )

        assert result.exit_code == 1
        assert "not available for BYOS" in result.output


@pytest.mark.unit
class TestBYOSGuardFirewall:
    def test_byos_firewall_blocked(self, tmp_path):
        """Firewall commands should be blocked for BYOS deployments."""
        from click.testing import CliRunner

        from dango.cli.commands.remote import remote

        cfg = _make_byos_config()

        with (
            patch(_PATCH_REQUIRE, return_value=(cfg, tmp_path)),
            patch("dango.cli.utils.find_project_root", return_value=tmp_path),
        ):
            runner = CliRunner()
            result = runner.invoke(remote, ["firewall", "list"], obj={"project_root": tmp_path})

        assert result.exit_code == 1
        assert "ufw" in result.output.lower()
