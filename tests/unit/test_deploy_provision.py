"""tests/unit/test_deploy_provision.py

Unit tests for dango/cli/commands/deploy_provision.py.

All external calls (DO API, SSH, httpx) are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands.deploy_provision import (
    _create_admin_and_enable_auth,
    _extract_ip,
    _health_check,
    _push_secrets,
    _ResourceTracker,
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


def _make_mock_ssh():
    ssh = MagicMock()
    result = MagicMock()
    result.success = True
    result.stdout = ""
    result.stderr = ""
    result.exit_code = 0
    ssh.exec_command.return_value = result
    return ssh


# ---------------------------------------------------------------------------
# 1. Resource tracker
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResourceTracker:
    def test_cleanup_droplet_only(self):
        """Cleanup deletes droplet when it's the only resource."""
        client = MagicMock()
        tracker = _ResourceTracker(client=client, droplet_id=123)
        errors = tracker.cleanup()
        assert errors == []
        client.delete_droplet.assert_called_once_with(123)

    def test_cleanup_all_resources(self):
        """Cleanup deletes all tracked resources."""
        client = MagicMock()
        spaces = MagicMock()
        tracker = _ResourceTracker(
            client=client,
            droplet_id=123,
            firewall_id="fw-456",
            ssh_key_id=789,
            spaces_bucket="bucket",
            spaces_client=spaces,
        )
        errors = tracker.cleanup()
        assert errors == []
        client.delete_droplet.assert_called_once_with(123)
        client.delete_firewall.assert_called_once_with("fw-456")
        client.delete_ssh_key.assert_called_once_with(789)
        spaces.delete_bucket.assert_called_once()

    def test_cleanup_on_exception_collects_errors(self):
        """Cleanup collects error messages when deletion fails."""
        client = MagicMock()
        client.delete_droplet.side_effect = RuntimeError("API timeout")
        tracker = _ResourceTracker(client=client, droplet_id=123)
        errors = tracker.cleanup()
        assert len(errors) == 1
        assert "Droplet 123" in errors[0]

    def test_cleanup_partial_errors(self):
        """Cleanup continues after partial failure."""
        client = MagicMock()
        client.delete_droplet.side_effect = RuntimeError("fail")
        client.delete_firewall.return_value = None
        tracker = _ResourceTracker(
            client=client,
            droplet_id=123,
            firewall_id="fw-456",
        )
        errors = tracker.cleanup()
        assert len(errors) == 1
        client.delete_firewall.assert_called_once()

    def test_cleanup_empty_tracker(self):
        """Cleanup on empty tracker does nothing."""
        tracker = _ResourceTracker()
        errors = tracker.cleanup()
        assert errors == []


# ---------------------------------------------------------------------------
# 2. IP extraction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractIP:
    def test_extracts_public_ip(self):
        """Extracts public IPv4 from droplet dict."""
        droplet = {
            "networks": {
                "v4": [
                    {"type": "private", "ip_address": "10.0.0.1"},
                    {"type": "public", "ip_address": "203.0.113.1"},
                ]
            }
        }
        assert _extract_ip(droplet) == "203.0.113.1"

    def test_missing_public_ip_raises(self):
        """Raises RuntimeError when no public IP."""
        droplet = {"networks": {"v4": [{"type": "private", "ip_address": "10.0.0.1"}]}}
        with pytest.raises(RuntimeError, match="no public IPv4"):
            _extract_ip(droplet)


# ---------------------------------------------------------------------------
# 3. Secrets push
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSecretsPush:
    def test_push_both_files(self, project_root):
        """Pushes both .dlt/secrets.toml and .env."""
        dlt_dir = project_root / ".dlt"
        dlt_dir.mkdir()
        (dlt_dir / "secrets.toml").write_text("[sources]\nkey = 'value'\n")
        (project_root / ".env").write_text("DB_URL=postgres://...\n")

        ssh = _make_mock_ssh()
        warnings: list[str] = []
        _push_secrets(ssh, project_root, warnings)

        assert warnings == []
        assert ssh.write_remote_file.call_count == 2

    def test_env_missing_continues(self, project_root):
        """Missing .env is silently skipped."""
        dlt_dir = project_root / ".dlt"
        dlt_dir.mkdir()
        (dlt_dir / "secrets.toml").write_text("[sources]\n")

        ssh = _make_mock_ssh()
        warnings: list[str] = []
        _push_secrets(ssh, project_root, warnings)

        assert warnings == []
        # Only secrets.toml written
        assert ssh.write_remote_file.call_count == 1

    def test_secrets_missing_warns(self, project_root):
        """Missing .dlt/secrets.toml produces a warning."""
        ssh = _make_mock_ssh()
        warnings: list[str] = []
        _push_secrets(ssh, project_root, warnings)

        assert len(warnings) == 1
        assert "secrets.toml" in warnings[0]


# ---------------------------------------------------------------------------
# 4. Admin creation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdminCreation:
    def test_creates_admin_via_ssh(self):
        """Admin creation sends base64-encoded Python script over SSH."""
        ssh = _make_mock_ssh()
        token = _create_admin_and_enable_auth(ssh, "admin@test.com", "password123")

        assert token  # non-empty token returned
        # exec_command called for Python script + chown
        assert ssh.exec_command.call_count >= 1
        # write_remote_file called for auth.yml
        ssh.write_remote_file.assert_called()

    def test_invalid_email_raises(self):
        """Invalid email format raises ValueError."""
        ssh = _make_mock_ssh()
        with pytest.raises(ValueError, match="Invalid email"):
            _create_admin_and_enable_auth(ssh, "not-an-email", "password123")

    def test_auth_yml_written(self):
        """auth.yml is written with 'enabled: true'."""
        ssh = _make_mock_ssh()
        _create_admin_and_enable_auth(ssh, "admin@test.com", "password123")

        # Find the write_remote_file call for auth.yml
        found = False
        for call in ssh.write_remote_file.call_args_list:
            if "auth.yml" in str(call):
                assert "enabled: true" in str(call)
                found = True
        assert found, "auth.yml write not found"


# ---------------------------------------------------------------------------
# 5. Health check
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHealthCheck:
    @patch("httpx.get")
    @patch("dango.cli.commands.deploy_provision.time.sleep")
    def test_immediate_success(self, mock_sleep, mock_get):
        """Health check returns True on immediate 200."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        assert _health_check("http://1.2.3.4", timeout=6, interval=3) is True
        mock_sleep.assert_not_called()

    @patch("httpx.get")
    @patch("dango.cli.commands.deploy_provision.time.sleep")
    def test_retry_then_success(self, mock_sleep, mock_get):
        """Health check retries and succeeds on third attempt."""
        fail_resp = MagicMock()
        fail_resp.status_code = 502
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        mock_get.side_effect = [fail_resp, fail_resp, ok_resp]

        assert _health_check("http://1.2.3.4", timeout=15, interval=3) is True

    @patch("httpx.get")
    @patch("dango.cli.commands.deploy_provision.time.sleep")
    def test_persistent_failure(self, mock_sleep, mock_get):
        """Health check returns False after timeout."""
        mock_get.side_effect = ConnectionError("refused")

        assert _health_check("http://1.2.3.4", timeout=6, interval=3) is False


# ---------------------------------------------------------------------------
# 6. Full provisioning sequence (integration-like, all mocked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProvisionSequence:
    @patch("httpx.get")
    @patch("dango.cli.commands.deploy_provision.time.sleep")
    def test_full_success(self, mock_sleep, mock_get, project_root, monkeypatch):
        """Full provisioning completes successfully with all steps mocked."""
        from dango.cli.commands.deploy_provision import run_provisioning
        from dango.cli.commands.deploy_wizard import WizardConfig

        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")

        config = WizardConfig(
            region="nyc1",
            size_slug="s-2vcpu-4gb",
            size_tier=None,
            domain=None,
            admin_email="admin@test.com",
            admin_password="strongpassword123",
            skip_oauth=True,
            enable_backups=False,
            skip_initial_sync=True,
            monthly_cost=24,
        )

        # Create .dlt/secrets.toml
        dlt_dir = project_root / ".dlt"
        dlt_dir.mkdir()
        (dlt_dir / "secrets.toml").write_text("[sources]\n")

        mock_client = MagicMock()
        mock_client.upload_ssh_key.return_value = {"ssh_key": {"id": 111}}
        mock_ssh = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_result.exit_code = 0
        mock_ssh.exec_command.return_value = mock_result

        droplet_dict = {
            "id": 222,
            "networks": {"v4": [{"type": "public", "ip_address": "1.2.3.4"}]},
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        with (
            patch(
                "dango.platform.cloud.digitalocean.DigitalOceanClient",
                return_value=mock_client,
            ),
            patch(
                "dango.platform.cloud.ssh.SSHManager",
                return_value=mock_ssh,
            ),
            patch(
                "dango.platform.cloud.provisioning.provision_droplet",
                return_value=droplet_dict,
            ),
            patch(
                "dango.platform.cloud.firewall.create_default_firewall",
                return_value={"id": "fw-333"},
            ),
            patch("dango.platform.cloud.server_setup.setup_server") as mock_setup,
            patch("dango.platform.cloud.file_sync.sync_project_files"),
            patch("dango.platform.cloud.provisioning.save_provisioning_metadata"),
        ):
            mock_setup.return_value = MagicMock(warnings=[])
            mock_ssh.generate_key_pair.return_value = "ssh-ed25519 AAAA..."

            result = run_provisioning(project_root, config)

        assert result.droplet_ip == "1.2.3.4"
        assert result.droplet_id == 222
        assert result.firewall_id == "fw-333"

    @patch("dango.cli.commands.deploy_provision.time.sleep")
    def test_provision_failure_cleanup(self, mock_sleep, project_root, monkeypatch):
        """Provisioning failure triggers resource cleanup."""
        from dango.cli.commands.deploy_provision import run_provisioning
        from dango.cli.commands.deploy_wizard import WizardConfig

        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")

        config = WizardConfig(
            region="nyc1",
            size_slug="s-2vcpu-4gb",
            size_tier=None,
            domain=None,
            admin_email="admin@test.com",
            admin_password="strongpassword123",
            skip_oauth=True,
            enable_backups=False,
            skip_initial_sync=True,
            monthly_cost=24,
        )

        mock_client = MagicMock()
        mock_client.upload_ssh_key.return_value = {"ssh_key": {"id": 111}}
        mock_ssh = MagicMock()
        mock_ssh.generate_key_pair.return_value = "ssh-ed25519 AAAA..."

        with (
            patch(
                "dango.platform.cloud.digitalocean.DigitalOceanClient",
                return_value=mock_client,
            ),
            patch(
                "dango.platform.cloud.ssh.SSHManager",
                return_value=mock_ssh,
            ),
            patch(
                "dango.platform.cloud.provisioning.provision_droplet",
                side_effect=RuntimeError("API error"),
            ),
            pytest.raises(SystemExit),
        ):
            run_provisioning(project_root, config)

        # SSH key was created, so cleanup should try to delete it
        mock_client.delete_ssh_key.assert_called_once_with(111)
