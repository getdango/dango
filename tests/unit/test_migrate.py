"""tests/unit/test_migrate.py

Unit tests for dango/platform/cloud/migrate.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudError, CloudProvisioningError
from dango.platform.cloud.ssh import CommandResult

# Patch paths — lazy imports, patch at source.
_PATCH_BACKUP = "dango.platform.cloud.backup.create_backup"
_PATCH_RESTORE = "dango.platform.cloud.backup.restore_from_archive"
_PATCH_PROVISION = "dango.platform.cloud.provisioning.provision_droplet"
_PATCH_SAVE_META = "dango.platform.cloud.provisioning.save_provisioning_metadata"
_PATCH_SETUP = "dango.platform.cloud.server_setup.setup_server"
_PATCH_SET_DOMAIN = "dango.platform.cloud.domain.set_domain"
_PATCH_SSH_CLS = "dango.platform.cloud.ssh.SSHManager"

# Private helpers in migrate.py — these are module-level, NOT lazy.
_PATCH_UPLOAD = "dango.platform.cloud.migrate._upload_backup_to_spaces"
_PATCH_DOWNLOAD = "dango.platform.cloud.migrate._download_backup_from_spaces"


def _make_ssh_mock(
    *,
    exec_results: dict[str, tuple[str, str, int]] | None = None,
) -> MagicMock:
    """Return a mock SSHManager with configurable exec_command results."""
    results = exec_results or {}

    def _exec_side_effect(command: str, timeout: int | None = None) -> CommandResult:
        for substr, (stdout, stderr, exit_code) in results.items():
            if substr in command:
                return CommandResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec_side_effect
    ssh.write_remote_file = MagicMock()
    ssh.connect = MagicMock()
    ssh.disconnect = MagicMock()
    return ssh


def _make_cloud_config(
    droplet_id: int = 42,
    droplet_ip: str = "1.2.3.4",
    region: str = "nyc1",
    size: str = "s-2vcpu-4gb",
    domain: str | None = None,
    firewall_id: str | None = "fw-abc",
    ssh_key_path: str = ".dango/cloud_key",
    ssh_key_id: int | None = 100,
    spaces: Any | None = None,
) -> MagicMock:
    """Return a mock CloudConfig."""
    cfg = MagicMock()
    cfg.droplet_id = droplet_id
    cfg.droplet_ip = droplet_ip
    cfg.region = region
    cfg.size = size
    cfg.domain = domain
    cfg.firewall_id = firewall_id
    cfg.ssh_key_path = ssh_key_path
    cfg.ssh_key_id = ssh_key_id
    if spaces is None:
        spaces_cfg = MagicMock()
        spaces_cfg.bucket = "test-bucket"
        spaces_cfg.region = "nyc3"
        spaces_cfg.access_key_env = "SPACES_ACCESS_KEY"
        spaces_cfg.secret_key_env = "SPACES_SECRET_KEY"
        cfg.spaces = spaces_cfg
    else:
        cfg.spaces = spaces
    return cfg


def _make_do_client() -> MagicMock:
    """Return a mock DigitalOceanClient."""
    client = MagicMock()
    client.get_firewall.return_value = {
        "name": "dango-fw",
        "droplet_ids": [42],
        "inbound_rules": [],
        "outbound_rules": [],
    }
    client.delete_droplet = MagicMock()
    return client


def _make_new_droplet(droplet_id: int = 99, ip: str = "5.6.7.8") -> dict[str, Any]:
    """Return a minimal droplet dict for the new server."""
    return {
        "id": droplet_id,
        "networks": {
            "v4": [{"ip_address": ip, "type": "public"}],
        },
    }


@pytest.mark.unit
class TestUploadBackupToSpaces:
    """Tests for _upload_backup_to_spaces()."""

    def test_uploads_and_returns_key(self) -> None:
        from dango.platform.cloud.migrate import _upload_backup_to_spaces

        ssh = _make_ssh_mock()
        spaces_cfg = MagicMock()
        spaces_cfg.bucket = "test-bucket"
        spaces_cfg.region = "nyc3"
        spaces_cfg.access_key_env = "SPACES_ACCESS_KEY"
        spaces_cfg.secret_key_env = "SPACES_SECRET_KEY"

        key = _upload_backup_to_spaces(
            ssh, "/srv/dango/backups/deploy/backup-test.tar.gz", spaces_cfg
        )
        assert key == "migration/backup-test.tar.gz"
        # Script written to file, then executed + cleaned up
        ssh.write_remote_file.assert_called_once()
        assert ssh.exec_command.call_count == 2  # exec + rm cleanup

    def test_raises_on_failure(self) -> None:
        from dango.platform.cloud.migrate import _upload_backup_to_spaces

        ssh = _make_ssh_mock(exec_results={"python": ("", "error", 1)})
        spaces_cfg = MagicMock()
        spaces_cfg.bucket = "test-bucket"
        spaces_cfg.region = "nyc3"
        spaces_cfg.access_key_env = "SPACES_ACCESS_KEY"
        spaces_cfg.secret_key_env = "SPACES_SECRET_KEY"

        with pytest.raises(CloudProvisioningError, match="upload backup"):
            _upload_backup_to_spaces(
                ssh, "/srv/dango/backups/deploy/backup-test.tar.gz", spaces_cfg
            )


@pytest.mark.unit
class TestDownloadBackupFromSpaces:
    """Tests for _download_backup_from_spaces()."""

    def test_downloads_and_returns_path(self) -> None:
        from dango.platform.cloud.migrate import _download_backup_from_spaces

        ssh = _make_ssh_mock()
        spaces_cfg = MagicMock()
        spaces_cfg.bucket = "test-bucket"
        spaces_cfg.region = "nyc3"
        spaces_cfg.access_key_env = "SPACES_ACCESS_KEY"
        spaces_cfg.secret_key_env = "SPACES_SECRET_KEY"

        path = _download_backup_from_spaces(ssh, "migration/backup-test.tar.gz", spaces_cfg)
        assert path == "/srv/dango/backups/deploy/backup-test.tar.gz"


@pytest.mark.unit
class TestCopySecretsBetweenServers:
    """Tests for _copy_secrets_between_servers()."""

    def test_copies_files(self) -> None:
        from dango.platform.cloud.migrate import _copy_secrets_between_servers

        old_ssh = _make_ssh_mock(
            exec_results={
                "cat /srv/dango/project/.env": ("DB_URL=test", "", 0),
                "cat /srv/dango/project/.dlt/secrets.toml": ("[sources]", "", 0),
            }
        )
        new_ssh = _make_ssh_mock()

        warnings = _copy_secrets_between_servers(old_ssh, new_ssh)
        assert len(warnings) == 0
        assert new_ssh.write_remote_file.call_count == 2

    def test_warns_on_missing_file(self) -> None:
        from dango.platform.cloud.migrate import _copy_secrets_between_servers

        old_ssh = _make_ssh_mock(
            exec_results={
                "cat /srv/dango/project/.env": ("", "", 1),
                "cat /srv/dango/project/.dlt/secrets.toml": ("", "", 1),
            }
        )
        new_ssh = _make_ssh_mock()

        warnings = _copy_secrets_between_servers(old_ssh, new_ssh)
        assert len(warnings) == 2
        assert new_ssh.write_remote_file.call_count == 0


@pytest.mark.unit
class TestUpdateFirewallDroplets:
    """Tests for _update_firewall_droplets()."""

    def test_swaps_droplet_ids(self) -> None:
        from dango.platform.cloud.migrate import _update_firewall_droplets

        client = _make_do_client()

        _update_firewall_droplets(client, "fw-abc", old_droplet_id=42, new_droplet_id=99)

        client.update_firewall.assert_called_once()
        call_kwargs = client.update_firewall.call_args[1]
        assert 42 not in call_kwargs["droplet_ids"]
        assert 99 in call_kwargs["droplet_ids"]


def _setup_migrate_patches(
    tmp_path: Path,
    *,
    new_ssh: MagicMock | None = None,
    health_ok: bool = True,
    setup_error: Exception | None = None,
) -> dict[str, Any]:
    """Return a dict of common patch context managers for migrate_server tests."""
    backup_result = MagicMock()
    backup_result.archive_path = "/srv/dango/backups/deploy/backup-test.tar.gz"

    key_dir = tmp_path / ".dango"
    key_dir.mkdir(parents=True, exist_ok=True)
    (key_dir / "cloud_key").write_text("dummy-key")

    if new_ssh is None:
        health_result = ("ok", "", 0) if health_ok else ("", "", 1)
        new_ssh = _make_ssh_mock(exec_results={"curl -sf": health_result})

    patches: dict[str, Any] = {
        "backup": patch(_PATCH_BACKUP, return_value=backup_result),
        "upload": patch(_PATCH_UPLOAD, return_value="migration/backup-test.tar.gz"),
        "provision": patch(_PATCH_PROVISION, return_value=_make_new_droplet()),
        "ssh_cls": patch(_PATCH_SSH_CLS, return_value=new_ssh),
        "download": patch(
            _PATCH_DOWNLOAD, return_value="/srv/dango/backups/deploy/backup-test.tar.gz"
        ),
        "restore": patch(_PATCH_RESTORE),
        "save_meta": patch(_PATCH_SAVE_META),
    }

    if setup_error:
        patches["setup"] = patch(_PATCH_SETUP, side_effect=setup_error)
    else:
        patches["setup"] = patch(_PATCH_SETUP)

    return patches


@pytest.mark.unit
class TestMigrateServer:
    """Tests for migrate_server()."""

    def test_requires_spaces_configured(self, tmp_path: Path) -> None:
        from dango.platform.cloud.migrate import migrate_server

        client = _make_do_client()
        ssh = _make_ssh_mock()
        config = _make_cloud_config()
        config.spaces = None

        with pytest.raises(CloudError, match="requires Spaces"):
            migrate_server(client, ssh, config, "s-4vcpu-8gb", "nyc1", project_root=tmp_path)

    def test_full_migration_happy_path(self, tmp_path: Path) -> None:
        from dango.platform.cloud.migrate import migrate_server

        old_ssh = _make_ssh_mock(
            exec_results={
                "cat /srv/dango/project/.env": ("KEY=val", "", 0),
                "cat /srv/dango/project/.dlt/secrets.toml": ("[src]", "", 0),
            }
        )
        config = _make_cloud_config(domain=None, firewall_id=None)
        client = _make_do_client()
        patches = _setup_migrate_patches(tmp_path)

        with (
            patches["backup"],
            patches["upload"],
            patches["provision"],
            patches["ssh_cls"],
            patches["setup"],
            patches["download"],
            patches["restore"],
            patches["save_meta"],
        ):
            result = migrate_server(
                client,
                old_ssh,
                config,
                "s-4vcpu-8gb",
                "sfo3",
                project_root=tmp_path,
            )

        assert result.new_droplet_id == 99
        assert result.new_droplet_ip == "5.6.7.8"
        assert result.new_region == "sfo3"
        assert result.new_size == "s-4vcpu-8gb"
        assert result.old_droplet_destroyed is True

    def test_keeps_both_on_health_failure(self, tmp_path: Path) -> None:
        from dango.platform.cloud.migrate import migrate_server

        old_ssh = _make_ssh_mock()
        config = _make_cloud_config(domain=None, firewall_id=None)
        client = _make_do_client()

        new_ssh = _make_ssh_mock(exec_results={"curl -sf": ("", "", 1)})
        patches = _setup_migrate_patches(tmp_path, new_ssh=new_ssh, health_ok=False)

        with (
            patches["backup"],
            patches["upload"],
            patches["provision"],
            patches["ssh_cls"],
            patches["setup"],
            patches["download"],
            patches["restore"],
            patch("dango.platform.cloud.backup.time.sleep"),
            patch(
                "dango.platform.cloud.migrate.time.monotonic",
                side_effect=[0.0, 100.0],
            ),
            patch(
                "dango.platform.cloud.backup.time.monotonic",
                side_effect=[0.0, 1.0, 100.0, 100.0],
            ),
        ):
            result = migrate_server(
                client,
                old_ssh,
                config,
                "s-4vcpu-8gb",
                "sfo3",
                project_root=tmp_path,
            )

        assert result.old_droplet_destroyed is False
        client.delete_droplet.assert_not_called()
        assert any("Health check failed" in w for w in result.warnings)

    def test_cleans_up_new_droplet_on_failure(self, tmp_path: Path) -> None:
        from dango.platform.cloud.migrate import migrate_server

        old_ssh = _make_ssh_mock()
        config = _make_cloud_config()
        client = _make_do_client()

        patches = _setup_migrate_patches(
            tmp_path, setup_error=CloudProvisioningError("Setup failed")
        )

        with (
            patches["backup"],
            patches["upload"],
            patches["provision"],
            patches["ssh_cls"],
            patches["setup"],
            pytest.raises(CloudProvisioningError, match="Setup failed"),
        ):
            migrate_server(
                client,
                old_ssh,
                config,
                "s-4vcpu-8gb",
                "sfo3",
                project_root=tmp_path,
            )

        client.delete_droplet.assert_called_once_with(99)

    def test_domain_update_on_migration(self, tmp_path: Path) -> None:
        from dango.platform.cloud.migrate import migrate_server

        old_ssh = _make_ssh_mock()
        config = _make_cloud_config(domain="example.com", firewall_id=None)
        client = _make_do_client()
        patches = _setup_migrate_patches(tmp_path)

        with (
            patches["backup"],
            patches["upload"],
            patches["provision"],
            patches["ssh_cls"],
            patches["setup"],
            patches["download"],
            patches["restore"],
            patch(_PATCH_SET_DOMAIN) as mock_domain,
            patches["save_meta"],
        ):
            result = migrate_server(
                client,
                old_ssh,
                config,
                "s-4vcpu-8gb",
                "sfo3",
                project_root=tmp_path,
            )

        assert result.dns_updated is True
        mock_domain.assert_called_once()

    def test_firewall_update_on_migration(self, tmp_path: Path) -> None:
        from dango.platform.cloud.migrate import migrate_server

        old_ssh = _make_ssh_mock()
        config = _make_cloud_config(domain=None, firewall_id="fw-abc")
        client = _make_do_client()
        patches = _setup_migrate_patches(tmp_path)

        with (
            patches["backup"],
            patches["upload"],
            patches["provision"],
            patches["ssh_cls"],
            patches["setup"],
            patches["download"],
            patches["restore"],
            patches["save_meta"],
        ):
            migrate_server(
                client,
                old_ssh,
                config,
                "s-4vcpu-8gb",
                "sfo3",
                project_root=tmp_path,
            )

        client.update_firewall.assert_called_once()
