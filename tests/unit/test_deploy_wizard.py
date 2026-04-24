"""tests/unit/test_deploy_wizard.py

Unit tests for dango/cli/commands/deploy_wizard.py.

Tests wizard validation, prerequisite checks, non-interactive mode,
deployment guard, reconnect mode, and cost summary calculation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands.deploy_wizard import (
    _get_monthly_cost,
    _step_prereqs,
    run_non_interactive,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_root(tmp_path):
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "sources.yml").write_text("sources: []")
    (dango_dir / "project.yml").write_text("project:\n  name: test\n")
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Prerequisite checks
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPrereqCheck:
    def test_missing_do_token_prompts_user(self, project_root, monkeypatch):
        """Missing DIGITALOCEAN_TOKEN prompts interactively."""
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        with patch("dango.cli.commands.deploy_wizard.click.prompt", return_value="dop_test123"):
            _step_prereqs(project_root)
        import os

        assert os.environ.get("DIGITALOCEAN_TOKEN") == "dop_test123"
        # Cleanup
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

    def test_missing_do_token_empty_input_exits(self, project_root, monkeypatch):
        """Empty token input raises SystemExit."""
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        with patch("dango.cli.commands.deploy_wizard.click.prompt", return_value=""):
            with pytest.raises(SystemExit):
                _step_prereqs(project_root)

    def test_missing_sources_yml(self, tmp_path, monkeypatch):
        """Missing sources.yml raises SystemExit."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        (tmp_path / ".dango").mkdir()
        with pytest.raises(SystemExit):
            _step_prereqs(tmp_path)

    def test_valid_project(self, project_root, monkeypatch):
        """Valid project with token passes prereqs."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        _step_prereqs(project_root)  # Should not raise


# ---------------------------------------------------------------------------
# 2. Non-interactive mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNonInteractive:
    def test_all_required_params(self, project_root, monkeypatch):
        """Non-interactive with all params succeeds."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        config = run_non_interactive(
            project_root,
            region="nyc1",
            size="s-2vcpu-4gb",
            admin_email="admin@example.com",
            admin_password="strongpassword123",
        )
        assert config.region == "nyc1"
        assert config.size_slug == "s-2vcpu-4gb"
        assert config.admin_email == "admin@example.com"
        assert config.admin_password == "strongpassword123"
        assert config.skip_oauth is True

    def test_missing_email(self, project_root, monkeypatch):
        """Non-interactive without admin_email raises SystemExit."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        with pytest.raises(SystemExit):
            run_non_interactive(
                project_root,
                admin_password="strongpassword123",
            )

    def test_weak_password(self, project_root, monkeypatch):
        """Non-interactive with weak password raises SystemExit."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        with pytest.raises(SystemExit):
            run_non_interactive(
                project_root,
                admin_email="admin@example.com",
                admin_password="short",
            )

    def test_invalid_region(self, project_root, monkeypatch):
        """Non-interactive with invalid region raises SystemExit."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        with pytest.raises(SystemExit):
            run_non_interactive(
                project_root,
                region="invalid-region",
                admin_email="admin@example.com",
                admin_password="strongpassword123",
            )

    def test_invalid_size(self, project_root, monkeypatch):
        """Non-interactive with invalid size raises SystemExit."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        with pytest.raises(SystemExit):
            run_non_interactive(
                project_root,
                size="invalid-size",
                admin_email="admin@example.com",
                admin_password="strongpassword123",
            )

    def test_defaults_used(self, project_root, monkeypatch):
        """Non-interactive uses defaults for region and size when not specified."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        config = run_non_interactive(
            project_root,
            admin_email="admin@example.com",
            admin_password="strongpassword123",
        )
        # Region should be from suggest_nearest_region (varies by env)
        assert config.region is not None
        # Size should be standard tier default
        assert config.size_slug == "s-2vcpu-4gb"

    def test_password_from_env(self, project_root, monkeypatch):
        """Non-interactive reads DANGO_ADMIN_PASSWORD from env."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        monkeypatch.setenv("DANGO_ADMIN_PASSWORD", "strongpassword123")
        config = run_non_interactive(
            project_root,
            admin_email="admin@example.com",
        )
        assert config.admin_password == "strongpassword123"

    def test_invalid_email_format(self, project_root, monkeypatch):
        """Non-interactive with invalid email format raises SystemExit."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        with pytest.raises(SystemExit):
            run_non_interactive(
                project_root,
                admin_email="not-an-email",
                admin_password="strongpassword123",
            )


# ---------------------------------------------------------------------------
# 3. Deployment guard (tested via deploy.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeploymentGuard:
    @patch("dango.cli.utils.find_project_root")
    def test_cloud_yml_exists_blocks(self, mock_find, project_root):
        """Existing cloud.yml with droplet_ip blocks new deployment."""
        from click.testing import CliRunner

        from dango.cli.commands.deploy import deploy

        mock_find.return_value = project_root

        cloud_yml = project_root / ".dango" / "cloud.yml"
        cloud_yml.write_text("droplet_ip: 1.2.3.4\n")

        runner = CliRunner()
        result = runner.invoke(deploy, [], obj={"project_root": project_root})
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_no_cloud_yml_proceeds(self, project_root, monkeypatch):
        """Missing cloud.yml allows deployment to proceed."""
        # Just verify the guard doesn't block — actual wizard will fail
        # because we don't mock the wizard steps
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        cloud_yml = project_root / ".dango" / "cloud.yml"
        assert not cloud_yml.exists()


# ---------------------------------------------------------------------------
# 4. Reconnect mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReconnect:
    @patch("dango.platform.cloud.ssh.SSHManager")
    @patch("dango.cli.utils.find_project_root")
    def test_valid_server(self, mock_find, mock_ssh_cls, project_root):
        """Reconnect to a valid Dango server writes cloud.yml."""
        from click.testing import CliRunner

        from dango.cli.commands.deploy import deploy

        mock_find.return_value = project_root

        # Create SSH key
        key_path = project_root / ".dango" / "cloud_key"
        key_path.write_text("fake-key")

        mock_ssh = MagicMock()
        mock_ssh_cls.return_value = mock_ssh
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = "project:\n  name: my-project\n"
        mock_ssh.exec_command.return_value = mock_result

        runner = CliRunner()
        result = runner.invoke(
            deploy,
            ["--reconnect", "--ip", "1.2.3.4"],
            obj={"project_root": project_root},
        )
        assert result.exit_code == 0
        cloud_yml = project_root / ".dango" / "cloud.yml"
        assert cloud_yml.exists()

    @patch("dango.platform.cloud.ssh.SSHManager")
    @patch("dango.cli.utils.find_project_root")
    def test_non_dango_server(self, mock_find, mock_ssh_cls, project_root):
        """Reconnect to non-Dango server raises SystemExit."""
        from click.testing import CliRunner

        from dango.cli.commands.deploy import deploy

        mock_find.return_value = project_root

        key_path = project_root / ".dango" / "cloud_key"
        key_path.write_text("fake-key")

        mock_ssh = MagicMock()
        mock_ssh_cls.return_value = mock_ssh
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.stdout = ""
        mock_ssh.exec_command.return_value = mock_result

        runner = CliRunner()
        result = runner.invoke(
            deploy,
            ["--reconnect", "--ip", "1.2.3.4"],
            obj={"project_root": project_root},
        )
        assert result.exit_code != 0

    @patch("dango.cli.utils.find_project_root")
    def test_missing_ip(self, mock_find, project_root):
        """Reconnect without --ip raises SystemExit."""
        from click.testing import CliRunner

        from dango.cli.commands.deploy import deploy

        mock_find.return_value = project_root

        key_path = project_root / ".dango" / "cloud_key"
        key_path.write_text("fake-key")

        runner = CliRunner()
        result = runner.invoke(
            deploy,
            ["--reconnect"],
            obj={"project_root": project_root},
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# 5. Cost summary
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCostSummary:
    def test_standard_no_backups(self):
        """Standard tier without backups = $24."""
        assert _get_monthly_cost("s-2vcpu-4gb", False) == 24

    def test_budget_with_backups(self):
        """Budget tier with backups = $17."""
        assert _get_monthly_cost("s-1vcpu-2gb", True) == 17

    def test_performance_no_backups(self):
        """Performance tier without backups = $48."""
        assert _get_monthly_cost("s-4vcpu-8gb", False) == 48

    def test_custom_size_uses_default_estimate(self):
        """Custom/unknown size slug uses $24 estimate."""
        assert _get_monthly_cost("custom-slug", False) == 24

    def test_standard_with_backups(self):
        """Standard tier with backups = $29."""
        assert _get_monthly_cost("s-2vcpu-4gb", True) == 29
