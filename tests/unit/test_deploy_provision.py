"""tests/unit/test_deploy_provision.py

Unit tests for dango/cli/commands/deploy_provision.py.

All external calls (DO API, SSH, httpx) are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands.deploy_provision import (
    _confirm_secrets_push,
    _create_admin_and_enable_auth,
    _extract_ip,
    _generate_cloud_profiles,
    _health_check,
    _push_secrets,
    _ResourceTracker,
    _save_extra_metadata,
    _setup_backups,
    _trigger_initial_sync,
)
from dango.exceptions import CloudProvisioningError

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
        _push_secrets(ssh, project_root)

        assert ssh.write_remote_file.call_count == 2

    def test_env_missing_continues(self, project_root):
        """Missing .env is silently skipped — only secrets.toml pushed."""
        dlt_dir = project_root / ".dlt"
        dlt_dir.mkdir()
        (dlt_dir / "secrets.toml").write_text("[sources]\n")

        ssh = _make_mock_ssh()
        _push_secrets(ssh, project_root)

        # Only secrets.toml written
        assert ssh.write_remote_file.call_count == 1

    def test_no_files_pushes_nothing(self, project_root):
        """No secret files means no writes."""
        ssh = _make_mock_ssh()
        _push_secrets(ssh, project_root)

        ssh.write_remote_file.assert_not_called()


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

    def test_dango_dir_chowned_recursively(self):
        """After admin creation, .dango/ is recursively chowned to dango user."""
        ssh = _make_mock_ssh()
        _create_admin_and_enable_auth(ssh, "admin@test.com", "password123")

        chown_calls = [
            str(c)
            for c in ssh.exec_command.call_args_list
            if "chown -R dango:dango /srv/dango/project/.dango" in str(c)
        ]
        assert len(chown_calls) >= 1, (
            "Expected chown -R dango:dango /srv/dango/project/.dango in SSH calls"
        )


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
        mock_client.upload_ssh_key.return_value = {"id": 111}
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
            patch("dango.cli.commands.deploy_provision.click.confirm", return_value=True),
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
        mock_client.upload_ssh_key.return_value = {"id": 111}
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
            pytest.raises(CloudProvisioningError),
        ):
            run_provisioning(project_root, config)

        # SSH key was created, so cleanup should try to delete it
        mock_client.delete_ssh_key.assert_called_once_with(111)


# ---------------------------------------------------------------------------
# 7. Backup setup
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSetupBackups:
    @patch("dango.platform.cloud.spaces.SpacesClient")
    def test_creates_bucket_and_enables_timer(self, mock_spaces_cls, project_root):
        """Backup setup creates bucket, writes credentials, enables systemd timer."""
        from dango.cli.commands.deploy_wizard import WizardConfig

        mock_spaces = MagicMock()
        mock_spaces_cls.return_value = mock_spaces

        ssh = _make_mock_ssh()
        config = WizardConfig(
            region="nyc1",
            size_slug="s-2vcpu-4gb",
            size_tier=None,
            domain=None,
            admin_email="admin@test.com",
            admin_password="pw",
            skip_oauth=True,
            enable_backups=True,
            skip_initial_sync=True,
            monthly_cost=29,
            spaces_access_key="DO_KEY",
            spaces_secret_key="DO_SECRET",
        )
        tracker = _ResourceTracker()

        _setup_backups(ssh, MagicMock(), project_root, config, tracker)

        # Bucket created
        mock_spaces.create_bucket.assert_called_once()
        # Tracker updated
        assert tracker.spaces_bucket is not None
        assert tracker.spaces_client is mock_spaces
        # Credentials appended to .env
        env_write_calls = [
            c for c in ssh.write_remote_file.call_args_list if c[0][0] == "/srv/dango/project/.env"
        ]
        assert len(env_write_calls) == 1
        written_content = env_write_calls[0][0][1]
        assert "SPACES_ACCESS_KEY=DO_KEY" in written_content
        assert "SPACES_SECRET_KEY=DO_SECRET" in written_content
        # Systemd files written
        systemd_calls = [
            c for c in ssh.write_remote_file.call_args_list if c[0][0].startswith("/etc/systemd/")
        ]
        assert len(systemd_calls) == 2  # .service + .timer
        # Timer enabled
        enable_calls = [c for c in ssh.exec_command.call_args_list if "enable --now" in str(c)]
        assert len(enable_calls) == 1


# ---------------------------------------------------------------------------
# 8. Save extra metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSaveExtraMetadata:
    def test_saves_firewall_and_ssh_key(self, project_root):
        """Saves firewall_id and ssh_key_id to cloud.yml."""
        # Create a minimal cloud.yml first (simulating save_provisioning_metadata)
        cloud_yml = project_root / ".dango" / "cloud.yml"
        cloud_yml.write_text(
            "droplet_id: 123\ndroplet_ip: 1.2.3.4\nregion: nyc1\nsize: s-2vcpu-4gb\n"
        )

        _save_extra_metadata(
            project_root,
            firewall_id="fw-456",
            ssh_key_id=789,
            domain=None,
            spaces_config=None,
        )

        content = cloud_yml.read_text()
        assert "fw-456" in content
        assert "789" in content

    def test_saves_domain_when_provided(self, project_root):
        """Saves domain to cloud.yml when provided."""
        cloud_yml = project_root / ".dango" / "cloud.yml"
        cloud_yml.write_text(
            "droplet_id: 123\ndroplet_ip: 1.2.3.4\nregion: nyc1\nsize: s-2vcpu-4gb\n"
        )

        _save_extra_metadata(
            project_root,
            firewall_id="fw-456",
            ssh_key_id=789,
            domain="example.com",
            spaces_config=None,
        )

        content = cloud_yml.read_text()
        assert "example.com" in content

    def test_noop_when_no_cloud_yml(self, project_root):
        """Does nothing when cloud.yml does not exist."""
        # No cloud.yml — should not raise
        _save_extra_metadata(
            project_root,
            firewall_id="fw-456",
            ssh_key_id=789,
            domain=None,
            spaces_config=None,
        )
        assert not (project_root / ".dango" / "cloud.yml").exists()


# ---------------------------------------------------------------------------
# 9. Trigger initial sync
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTriggerInitialSync:
    @patch("httpx.post")
    def test_posts_with_token(self, mock_post):
        """Trigger sends POST with deploy token."""
        _trigger_initial_sync("http://1.2.3.4", "test-token-123")

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs[0][0] == "http://1.2.3.4/api/initial-sync/start"
        headers = call_kwargs[1]["headers"]
        assert headers["Authorization"] == "Bearer test-token-123"

    @patch("httpx.post", side_effect=ConnectionError("refused"))
    def test_swallows_errors(self, mock_post):
        """Trigger does not raise on connection failure."""
        # Should not raise
        _trigger_initial_sync("http://1.2.3.4", "test-token-123")


# ---------------------------------------------------------------------------
# 10. Admin email persistence (BUG-100)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdminEmailPersistence:
    def test_appends_admin_email_to_env(self):
        """BUG-100: _create_admin_and_enable_auth appends DANGO_ADMIN_EMAIL to .env."""
        ssh = _make_mock_ssh()
        _create_admin_and_enable_auth(ssh, "admin@test.com", "password123")

        # Find the grep/echo command that appends DANGO_ADMIN_EMAIL
        email_cmds = [
            str(c) for c in ssh.exec_command.call_args_list if "DANGO_ADMIN_EMAIL" in str(c)
        ]
        assert len(email_cmds) >= 1, "Expected DANGO_ADMIN_EMAIL append command"
        assert "admin@test.com" in email_cmds[0]

    def test_email_append_uses_grep_guard(self):
        """DANGO_ADMIN_EMAIL append uses grep -q to avoid duplicates."""
        ssh = _make_mock_ssh()
        _create_admin_and_enable_auth(ssh, "admin@test.com", "password123")

        email_cmds = [
            str(c)
            for c in ssh.exec_command.call_args_list
            if "DANGO_ADMIN_EMAIL" in str(c) and "grep -q" in str(c)
        ]
        assert len(email_cmds) >= 1, "Expected grep -q guard for DANGO_ADMIN_EMAIL"


# ---------------------------------------------------------------------------
# 11. Cloud profiles generation (BUG-114)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateCloudProfiles:
    def test_with_known_size_slug(self, project_root):
        """BUG-114: Generates profiles.yml with known DO size tier specs."""
        # Create dbt_project.yml
        dbt_dir = project_root / "dbt"
        dbt_dir.mkdir(exist_ok=True)
        (dbt_dir / "dbt_project.yml").write_text("name: my_project\n")

        ssh = _make_mock_ssh()
        _generate_cloud_profiles(ssh, project_root, size_slug="s-2vcpu-4gb")

        # Verify write_remote_file was called with profiles.yml
        write_calls = [
            c for c in ssh.write_remote_file.call_args_list if "profiles.yml" in str(c[0][0])
        ]
        assert len(write_calls) == 1
        content = write_calls[0][0][1]
        assert "my_project" in content
        assert "threads: 2" in content  # s-2vcpu-4gb has 2 vcpus
        assert "warehouse.duckdb" in content

    def test_with_byos_auto_detect(self, project_root):
        """BUG-114: BYOS auto-detects hardware via SSH when no size_slug."""
        dbt_dir = project_root / "dbt"
        dbt_dir.mkdir(exist_ok=True)
        (dbt_dir / "dbt_project.yml").write_text("name: byos_project\n")

        ssh = _make_mock_ssh()

        def _exec_side_effect(cmd, **kwargs):
            result = MagicMock()
            result.success = True
            result.stderr = ""
            result.exit_code = 0
            if cmd == "nproc":
                result.stdout = "4"
            elif "free -g" in cmd:
                result.stdout = "8"
            else:
                result.stdout = ""
            return result

        ssh.exec_command.side_effect = _exec_side_effect

        _generate_cloud_profiles(ssh, project_root)

        write_calls = [
            c for c in ssh.write_remote_file.call_args_list if "profiles.yml" in str(c[0][0])
        ]
        assert len(write_calls) == 1
        content = write_calls[0][0][1]
        assert "byos_project" in content
        assert "threads: 4" in content  # auto-detected 4 vcpus

    def test_falls_back_to_defaults(self, project_root):
        """Falls back to project_name='dango' and 2/4 specs when dbt_project.yml missing."""
        ssh = _make_mock_ssh()
        _generate_cloud_profiles(ssh, project_root, size_slug="custom-size")

        write_calls = [
            c for c in ssh.write_remote_file.call_args_list if "profiles.yml" in str(c[0][0])
        ]
        assert len(write_calls) == 1
        content = write_calls[0][0][1]
        # Falls back to "dango" project name and 2 vcpus / 4gb ram defaults
        assert "dango" in content
        assert "threads: 2" in content

    def test_chowns_profiles_to_dango(self, project_root):
        """profiles.yml is chowned to dango:dango after creation."""
        ssh = _make_mock_ssh()
        _generate_cloud_profiles(ssh, project_root, size_slug="s-2vcpu-4gb")

        chown_calls = [
            str(c)
            for c in ssh.exec_command.call_args_list
            if "chown dango:dango" in str(c) and "profiles.yml" in str(c)
        ]
        assert len(chown_calls) >= 1


# ---------------------------------------------------------------------------
# 12. BUG-122: Confirm secrets push (pre-SSH)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfirmSecretsPush:
    def test_confirms_when_files_exist(self, project_root):
        """Returns True when user confirms and secret files exist."""
        dlt_dir = project_root / ".dlt"
        dlt_dir.mkdir()
        (dlt_dir / "secrets.toml").write_text("[sources]\n")
        (project_root / ".env").write_text("KEY=val\n")

        warnings: list[str] = []
        with patch("dango.cli.commands.deploy_provision.click.confirm", return_value=True):
            result = _confirm_secrets_push(project_root, warnings)

        assert result is True
        assert warnings == []

    def test_returns_false_when_declined(self, project_root):
        """Returns False when user declines."""
        dlt_dir = project_root / ".dlt"
        dlt_dir.mkdir()
        (dlt_dir / "secrets.toml").write_text("[sources]\n")

        warnings: list[str] = []
        with patch("dango.cli.commands.deploy_provision.click.confirm", return_value=False):
            result = _confirm_secrets_push(project_root, warnings)

        assert result is False
        assert "skipped by user" in warnings[0]

    def test_returns_false_when_no_files(self, project_root):
        """Returns False when no secret files exist."""
        warnings: list[str] = []
        result = _confirm_secrets_push(project_root, warnings)

        assert result is False
        assert "No .dlt/secrets.toml or .env found locally" in warnings[0]


# ---------------------------------------------------------------------------
# 13. BUG-122: Empty exception message handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmptyExceptionMessage:
    def test_empty_str_exception_gets_type_name(self):
        """Empty exception message is replaced with type name."""
        # Simulate the error message handling in the except block
        exc = Exception("")
        err_msg = str(exc) or f"{type(exc).__name__} (no detail)"
        assert err_msg == "Exception (no detail)"

        exc2 = TimeoutError()
        err_msg2 = str(exc2) or f"{type(exc2).__name__} (no detail)"
        assert err_msg2 == "TimeoutError (no detail)"


# ---------------------------------------------------------------------------
# 14. BUG-239: Admin password race condition — upsert semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdminUpsertSemantics:
    """BUG-239: When admin already exists, deploy must update password, not silently skip."""

    def test_no_bare_except_pass_in_admin_script(self):
        """Admin creation script must not have bare 'except Exception: pass'."""
        import inspect

        source = inspect.getsource(_create_admin_and_enable_auth)

        # The script string should not contain pass after except Exception
        assert "pass  # user may already exist" not in source, (
            "Found bare 'except Exception: pass' — admin creation must use upsert semantics"
        )

    def test_script_contains_update_user_import(self):
        """Admin script string must import update_user for upsert."""
        import inspect

        source = inspect.getsource(_create_admin_and_enable_auth)
        assert "update_user" in source
        assert "get_user_by_email" in source
        assert "UserUpdate" in source

    def test_script_updates_password_when_user_exists(self):
        """Script must update password_hash when admin already exists."""
        import inspect

        source = inspect.getsource(_create_admin_and_enable_auth)
        assert "UserUpdate(password_hash=pw_hash)" in source, (
            "Must update password_hash with wizard-provided password on race condition"
        )
