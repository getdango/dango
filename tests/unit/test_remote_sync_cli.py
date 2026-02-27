"""tests/unit/test_remote_sync_cli.py

Tests for the ``dango remote sync`` CLI command (TASK-040c).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.main import cli

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PATCH_MGMT = "dango.cli.commands.remote_mgmt"
_PATCH_REMOTE_SYNC = "dango.cli.commands.remote_sync"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _make_cloud_cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.droplet_id = 12345
    cfg.droplet_ip = "1.2.3.4"
    cfg.ssh_key_path = ".dango/ssh/id_ed25519"
    return cfg


def _make_ssh_mock(stdout: str = "", stderr: str = "", success: bool = True) -> MagicMock:
    ssh = MagicMock()
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.success = success
    ssh.exec_command.return_value = result
    return ssh


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteSyncCommand:
    """Tests for dango remote sync."""

    @patch(f"{_PATCH_MGMT}._make_ssh_manager")
    @patch(f"{_PATCH_MGMT}._load_cloud_config_with_ip")
    def test_no_wait_triggers_background(self, mock_load, mock_ssh_maker, tmp_path):
        cloud_cfg = _make_cloud_cfg()
        mock_load.return_value = (cloud_cfg, tmp_path)
        ssh = _make_ssh_mock()
        mock_ssh_maker.return_value = ssh

        runner = CliRunner()
        result = runner.invoke(cli, ["remote", "sync", "my_source"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Sync triggered" in result.output
        ssh.connect.assert_called_once_with("1.2.3.4")
        ssh.disconnect.assert_called_once()
        # Verify nohup was used for background execution
        cmd_arg = ssh.exec_command.call_args[0][0]
        assert "nohup" in cmd_arg

    @patch(f"{_PATCH_MGMT}._make_ssh_manager")
    @patch(f"{_PATCH_MGMT}._load_cloud_config_with_ip")
    def test_wait_blocks_and_shows_result(self, mock_load, mock_ssh_maker, tmp_path):
        cloud_cfg = _make_cloud_cfg()
        mock_load.return_value = (cloud_cfg, tmp_path)

        json_result = json.dumps({"status": "success", "duration_seconds": 12.3, "record_id": 1})
        ssh = _make_ssh_mock(stdout=json_result)
        mock_ssh_maker.return_value = ssh

        runner = CliRunner()
        result = runner.invoke(
            cli, ["remote", "sync", "my_source", "--wait"], catch_exceptions=False
        )

        assert result.exit_code == 0
        plain = _ANSI_RE.sub("", result.output)
        assert "Sync completed" in plain
        assert "12.3s" in plain
        # Should not use nohup when --wait is set
        cmd_arg = ssh.exec_command.call_args[0][0]
        assert "nohup" not in cmd_arg

    @patch(f"{_PATCH_MGMT}._make_ssh_manager")
    @patch(f"{_PATCH_MGMT}._load_cloud_config_with_ip")
    def test_full_refresh_passed_through(self, mock_load, mock_ssh_maker, tmp_path):
        cloud_cfg = _make_cloud_cfg()
        mock_load.return_value = (cloud_cfg, tmp_path)
        ssh = _make_ssh_mock()
        mock_ssh_maker.return_value = ssh

        runner = CliRunner()
        result = runner.invoke(
            cli, ["remote", "sync", "my_source", "--full-refresh"], catch_exceptions=False
        )

        assert result.exit_code == 0
        cmd_arg = ssh.exec_command.call_args[0][0]
        # Parse the JSON payload from the command
        # The command looks like: nohup /srv/.../python3 -m ... '{...}' > ...
        assert '"full_refresh": true' in cmd_arg or '"full_refresh":true' in cmd_arg

    @patch(f"{_PATCH_MGMT}._make_ssh_manager")
    @patch(f"{_PATCH_MGMT}._load_cloud_config_with_ip")
    def test_backfill_parsed_and_sent(self, mock_load, mock_ssh_maker, tmp_path):
        cloud_cfg = _make_cloud_cfg()
        mock_load.return_value = (cloud_cfg, tmp_path)
        ssh = _make_ssh_mock()
        mock_ssh_maker.return_value = ssh

        runner = CliRunner()
        result = runner.invoke(
            cli, ["remote", "sync", "my_source", "--backfill", "7d"], catch_exceptions=False
        )

        assert result.exit_code == 0
        cmd_arg = ssh.exec_command.call_args[0][0]
        assert '"backfill_days": 7' in cmd_arg or '"backfill_days":7' in cmd_arg

    def test_invalid_backfill_errors(self):
        runner = CliRunner()
        # Even without cloud config, backfill validation should fail first
        # We need to mock cloud config loading to not fail before backfill check
        with (
            patch(f"{_PATCH_MGMT}._load_cloud_config_with_ip") as mock_load,
            patch(f"{_PATCH_MGMT}._make_ssh_manager"),
        ):
            mock_load.return_value = (_make_cloud_cfg(), Path("/fake"))
            result = runner.invoke(cli, ["remote", "sync", "my_source", "--backfill", "abc"])

        assert result.exit_code != 0
        assert "Invalid duration" in result.output

    @patch(f"{_PATCH_MGMT}._make_ssh_manager")
    @patch(f"{_PATCH_MGMT}._load_cloud_config_with_ip")
    def test_wait_failed_sync_exits_nonzero(self, mock_load, mock_ssh_maker, tmp_path):
        cloud_cfg = _make_cloud_cfg()
        mock_load.return_value = (cloud_cfg, tmp_path)

        json_result = json.dumps(
            {"status": "failed", "duration_seconds": 5, "record_id": 2, "error": "DB timeout"}
        )
        ssh = _make_ssh_mock(stdout=json_result)
        mock_ssh_maker.return_value = ssh

        runner = CliRunner()
        result = runner.invoke(cli, ["remote", "sync", "my_source", "--wait"])

        assert result.exit_code != 0
        assert "Sync failed" in result.output
        assert "DB timeout" in result.output
