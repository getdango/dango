"""tests/unit/test_remote_backup_cli.py

Unit tests for dango/cli/commands/remote_backup.py.

Uses Click's CliRunner with mocked SSH, SpacesClient, and ConfigLoader
to avoid any real network or filesystem access.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote import remote

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cloud_config(*, has_spaces: bool = True) -> MagicMock:
    """Return a mock CloudConfig."""
    cfg = MagicMock()
    cfg.provider = "digitalocean"
    cfg.droplet_id = 42
    cfg.droplet_ip = "1.2.3.4"
    cfg.firewall_id = "fw-abc"
    cfg.ssh_key_path = ".dango/cloud_key"
    cfg.region = "nyc1"
    if has_spaces:
        cfg.spaces = MagicMock()
        cfg.spaces.bucket = "my-bucket"
        cfg.spaces.region = "nyc3"
        cfg.spaces.access_key_env = "SPACES_ACCESS_KEY"
        cfg.spaces.secret_key_env = "SPACES_SECRET_KEY"
    else:
        cfg.spaces = None
    return cfg


def _make_loader(cloud_cfg: MagicMock | None = None) -> MagicMock:
    """Return a mock ConfigLoader."""
    if cloud_cfg is None:
        cloud_cfg = _make_cloud_config()
    loader = MagicMock()
    loader.load_cloud_config.return_value = cloud_cfg
    return loader


def _make_ssh_mock() -> MagicMock:
    """Return a mock SSHManager."""
    from dango.platform.cloud.ssh import CommandResult

    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
    ssh.connect.return_value = ssh
    ssh.disconnect.return_value = None
    return ssh


_PATCH_LOADER = "dango.config.loader.ConfigLoader"
_PATCH_REQUIRE_CTX = "dango.cli.utils.require_project_context"
_PATCH_SSH_MANAGER = "dango.platform.cloud.ssh.SSHManager"


def _run(args: list[str], tmp_path: Path, *, catch_exceptions: bool = False) -> Any:
    """Invoke ``remote`` CLI group with the given args."""
    runner = CliRunner()
    return runner.invoke(
        remote,
        args,
        obj={"project_root": tmp_path},
        catch_exceptions=catch_exceptions,
    )


# ---------------------------------------------------------------------------
# 1. backup (on-demand, no subcommand)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackupOnDemand:
    def test_runs_scheduled_backup_on_server(self, tmp_path):
        """Invokes scheduled_backup module on server via SSH."""
        from dango.platform.cloud.ssh import CommandResult

        ssh = _make_ssh_mock()
        ssh.exec_command.return_value = CommandResult(
            stdout="Backup complete", stderr="", exit_code=0
        )

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader()):
                with patch(_PATCH_SSH_MANAGER, return_value=ssh):
                    result = _run(["backup"], tmp_path)

        assert result.exit_code == 0
        assert "completed" in result.output.lower()
        ssh.disconnect.assert_called_once()

    def test_no_deployment_exits_with_error(self, tmp_path):
        """Exits when no cloud deployment is configured."""
        cfg = MagicMock()
        cfg.droplet_id = None
        cfg.droplet_ip = None

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader(cfg)):
                result = _run(["backup"], tmp_path)

        assert result.exit_code == 1
        assert "No cloud deployment" in result.output


# ---------------------------------------------------------------------------
# 2. backup list
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackupList:
    def test_shows_local_backups(self, tmp_path):
        """Lists local server backups in a table."""
        ssh = _make_ssh_mock()
        cloud_cfg = _make_cloud_config(has_spaces=False)

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader(cloud_cfg)):
                with patch(_PATCH_SSH_MANAGER, return_value=ssh):
                    with patch(
                        "dango.platform.cloud.backup.list_local_backups",
                        return_value=[
                            {
                                "name": "backup-20260224-143000.tar.gz",
                                "path": "/srv/dango/backups/deploy/backup-20260224-143000.tar.gz",
                                "size_bytes": 5242880,
                                "date": "20260224-143000",
                            }
                        ],
                    ):
                        result = _run(["backup", "list"], tmp_path)

        assert result.exit_code == 0
        assert "backup-20260224-143000" in result.output
        ssh.disconnect.assert_called_once()

    def test_empty_shows_no_backups(self, tmp_path):
        """Shows message when no backups exist."""
        ssh = _make_ssh_mock()
        cloud_cfg = _make_cloud_config(has_spaces=False)

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader(cloud_cfg)):
                with patch(_PATCH_SSH_MANAGER, return_value=ssh):
                    with patch(
                        "dango.platform.cloud.backup.list_local_backups",
                        return_value=[],
                    ):
                        result = _run(["backup", "list"], tmp_path)

        assert result.exit_code == 0
        assert "No backups found" in result.output


# ---------------------------------------------------------------------------
# 3. backup enable
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackupEnable:
    def test_writes_systemd_units_and_enables(self, tmp_path):
        """Writes timer + service files and enables timer."""
        from dango.platform.cloud.ssh import CommandResult

        ssh = _make_ssh_mock()
        # grep for SPACES_ACCESS_KEY succeeds
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader()):
                with patch(_PATCH_SSH_MANAGER, return_value=ssh):
                    result = _run(["backup", "enable"], tmp_path)

        assert result.exit_code == 0
        assert "enabled" in result.output.lower()
        # Verify systemd files were written
        assert ssh.write_remote_file.call_count == 2
        ssh.disconnect.assert_called_once()

    def test_missing_spaces_credentials_fails(self, tmp_path):
        """Fails when SPACES_ACCESS_KEY is not in .env."""
        from dango.platform.cloud.ssh import CommandResult

        ssh = _make_ssh_mock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=1)

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader()):
                with patch(_PATCH_SSH_MANAGER, return_value=ssh):
                    result = _run(["backup", "enable"], tmp_path)

        assert result.exit_code == 1
        assert "SPACES_ACCESS_KEY" in result.output


# ---------------------------------------------------------------------------
# 4. backup disable
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackupDisable:
    def test_disables_timer(self, tmp_path):
        """Disables the systemd backup timer."""
        ssh = _make_ssh_mock()

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader()):
                with patch(_PATCH_SSH_MANAGER, return_value=ssh):
                    result = _run(["backup", "disable"], tmp_path)

        assert result.exit_code == 0
        assert "disabled" in result.output.lower()
        ssh.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# 5. backup download
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackupDownload:
    def test_downloads_from_spaces(self, tmp_path):
        """Downloads a backup from Spaces to local path."""
        mock_client = MagicMock()
        mock_client.download.return_value = b"archive-data"

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader()):
                with patch(
                    "dango.platform.cloud.spaces.SpacesClient",
                    return_value=mock_client,
                ):
                    output_path = str(tmp_path / "downloaded.tar.gz")
                    result = _run(
                        ["backup", "download", "backup-20260224-143000.tar.gz", "-o", output_path],
                        tmp_path,
                    )

        assert result.exit_code == 0
        assert "Downloaded" in result.output

    def test_no_spaces_config_fails(self, tmp_path):
        """Fails when Spaces is not configured."""
        cloud_cfg = _make_cloud_config(has_spaces=False)

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader(cloud_cfg)):
                result = _run(
                    ["backup", "download", "backup-20260224-143000.tar.gz"],
                    tmp_path,
                )

        assert result.exit_code == 1
        assert "Spaces not configured" in result.output


# ---------------------------------------------------------------------------
# 6. backup restore
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackupRestore:
    def test_restore_prompts_for_confirmation(self, tmp_path):
        """Restore asks for confirmation before proceeding."""
        ssh = _make_ssh_mock()

        runner = CliRunner()
        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader()):
                with patch(_PATCH_SSH_MANAGER, return_value=ssh):
                    result = runner.invoke(
                        remote,
                        ["backup", "restore", "backup-20260224-143000.tar.gz"],
                        obj={"project_root": tmp_path},
                        input="n\n",
                    )

        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()


# ---------------------------------------------------------------------------
# 7. rollback command
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRollbackCommand:
    def test_rollback_prompts_for_confirmation(self, tmp_path):
        """rollback asks for confirmation before proceeding."""
        ssh = _make_ssh_mock()

        runner = CliRunner()
        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader()):
                with patch(_PATCH_SSH_MANAGER, return_value=ssh):
                    result = runner.invoke(
                        remote,
                        ["rollback"],
                        obj={"project_root": tmp_path},
                        input="n\n",
                    )

        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()

    def test_rollback_skip_prompt_with_yes(self, tmp_path):
        """rollback --yes skips confirmation prompt."""
        from dango.platform.cloud.backup import RestoreResult

        ssh = _make_ssh_mock()
        mock_result = RestoreResult(
            restored_from="/srv/dango/backups/deploy/backup-20260224-143000.tar.gz",
            services_restarted=True,
            health_check_passed=True,
            duration_seconds=15.0,
        )

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader()):
                with patch(_PATCH_SSH_MANAGER, return_value=ssh):
                    with patch(
                        "dango.platform.cloud.backup.rollback",
                        return_value=mock_result,
                    ):
                        result = _run(["rollback", "--yes"], tmp_path)

        assert result.exit_code == 0
        assert "complete" in result.output.lower()
        ssh.disconnect.assert_called_once()
