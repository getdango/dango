"""tests/unit/test_remote_backup_download.py

Unit tests for dango/cli/commands/remote_backup.py — download, verify-metabase,
and config subcommands.

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
# Helpers (shared with test_remote_backup_cli.py)
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
# 1. backup download --from-server
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackupDownloadFromServer:
    def test_downloads_via_ssh_and_sftp(self, tmp_path):
        """--from-server creates backup on server, downloads via SFTP."""
        import json

        from dango.platform.cloud.ssh import CommandResult

        ssh = _make_ssh_mock()
        create_output = json.dumps(
            {
                "path": "/srv/dango/backups/deploy/backup-20260224-143000.tar.gz",
                "warnings": [],
            }
        )
        ssh.exec_command.side_effect = [
            CommandResult(stdout="__BACKUP_RESULT__" + create_output, stderr="", exit_code=0),
            CommandResult(
                stdout="/dev/vda1 25000 15000 8000 40% /srv/dango\n",
                stderr="",
                exit_code=0,
            ),
        ]
        ssh.download_file = MagicMock()

        # Create output path so stat() succeeds after mock download
        output_path = tmp_path / "latest.tar.gz"
        output_path.write_text("fake-backup-data")

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader()):
                with patch(_PATCH_SSH_MANAGER, return_value=ssh):
                    result = _run(
                        ["backup", "download", "--from-server", "-o", str(output_path)],
                        tmp_path,
                    )

        assert result.exit_code == 0
        assert "Downloaded" in result.output
        ssh.download_file.assert_called_once()
        ssh.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# 2. backup verify-metabase
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackupVerifyMetabase:
    def test_h2_files_found_in_archive(self, tmp_path):
        """verify-metabase reports PASS when both H2 files exist in archive."""
        import io
        import tarfile

        # Create a .tar.gz with metabase/ directory
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            mv_info = tarfile.TarInfo(name="metabase/metabase.db.mv.db")
            mv_info.size = 1024
            tf.addfile(mv_info, io.BytesIO(b"x" * 1024))
            trace_info = tarfile.TarInfo(name="metabase/metabase.db.trace.db")
            trace_info.size = 512
            tf.addfile(trace_info, io.BytesIO(b"y" * 512))
        archive_data = buf.getvalue()

        mock_client = MagicMock()
        mock_client.download.return_value = archive_data

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader()):
                with patch(
                    "dango.platform.cloud.spaces.SpacesClient",
                    return_value=mock_client,
                ):
                    result = _run(
                        ["backup", "verify-metabase", "backup-20260224-143000.tar.gz"],
                        tmp_path,
                    )

        assert result.exit_code == 0
        assert "PASS" in result.output

    def test_h2_missing_in_archive(self, tmp_path):
        """verify-metabase reports FAIL when no H2 files in archive."""
        import io
        import tarfile

        # Create a .tar.gz without metabase/ directory
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="data/warehouse.duckdb")
            info.size = 100
            tf.addfile(info, io.BytesIO(b"z" * 100))
        archive_data = buf.getvalue()

        mock_client = MagicMock()
        mock_client.download.return_value = archive_data

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader()):
                with patch(
                    "dango.platform.cloud.spaces.SpacesClient",
                    return_value=mock_client,
                ):
                    result = _run(
                        ["backup", "verify-metabase", "backup-20260224-143000.tar.gz"],
                        tmp_path,
                    )

        assert result.exit_code == 0
        assert "FAIL" in result.output

    def test_live_check_success(self, tmp_path):
        """verify-metabase (no arg) checks live /api/health and reports PASS."""
        from dango.platform.cloud.ssh import CommandResult

        ssh = _make_ssh_mock()
        ssh.exec_command.return_value = CommandResult(
            stdout='{"status":"ok"}', stderr="", exit_code=0
        )

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader()):
                with patch(_PATCH_SSH_MANAGER, return_value=ssh):
                    result = _run(["backup", "verify-metabase"], tmp_path)

        assert result.exit_code == 0
        assert "PASS" in result.output
        ssh.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# 3. backup config
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackupConfig:
    def test_shows_retention_from_cloud_yml(self, tmp_path):
        """backup config shows retention settings from cloud.yml BackupConfig."""
        from dango.config.models import BackupConfig, SpacesRetentionConfig

        cfg = _make_cloud_config()
        cfg.backup = BackupConfig(
            include_secrets=True,
            on_server_retention=3,
            spaces_retention=SpacesRetentionConfig(daily=14, weekly=8, monthly=3),
        )

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader(cfg)):
                result = _run(["backup", "config"], tmp_path)

        assert result.exit_code == 0
        assert "14" in result.output
        assert "8" in result.output
        assert "3" in result.output

    def test_shows_defaults_when_backup_not_configured(self, tmp_path):
        """backup config shows defaults when backup: key is not in cloud.yml."""
        cfg = _make_cloud_config()
        cfg.backup = None

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=_make_loader(cfg)):
                result = _run(["backup", "config"], tmp_path)

        assert result.exit_code == 0
        assert "default" in result.output.lower()
