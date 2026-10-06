"""tests/unit/test_remote_sync_cli.py

Tests for the ``dango remote sync`` CLI command (TASK-040c).
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.main import cli
from dango.exceptions import CloudSSHError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PATCH_MGMT = "dango.cli.commands.remote_mgmt"
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
        # The command is `sh -c <script>`; the script guards `cd`, backgrounds only the sync
        cmd_arg = ssh.exec_command.call_args[0][0]
        assert cmd_arg.startswith("sh -c ")
        script = shlex.split(cmd_arg)[2]
        assert script.startswith("cd /srv/dango/project || {")
        assert "\nnohup sudo -u dango -H env DANGO_CLOUD_MODE=true" in script
        assert "nohup cd" not in script
        assert "> /dev/null 2>&1 &\n" in script
        assert ssh.exec_command.call_args[1] == {"timeout": 30, "check": False}

    @patch(f"{_PATCH_MGMT}._make_ssh_manager")
    @patch(f"{_PATCH_MGMT}._load_cloud_config_with_ip")
    def test_no_wait_failed_exec_exits_nonzero(self, mock_load, mock_ssh_maker, tmp_path):
        mock_load.return_value = (_make_cloud_cfg(), tmp_path)
        ssh = _make_ssh_mock(stderr="boom", success=False)
        mock_ssh_maker.return_value = ssh

        runner = CliRunner()
        result = runner.invoke(cli, ["remote", "sync", "my_source"])

        assert result.exit_code == 1
        assert "Sync triggered" not in result.output
        assert "boom" in result.output
        ssh.disconnect.assert_called_once()

    @patch(f"{_PATCH_MGMT}._make_ssh_manager")
    @patch(f"{_PATCH_MGMT}._load_cloud_config_with_ip")
    def test_no_wait_early_exit_reports_remote_message(self, mock_load, mock_ssh_maker, tmp_path):
        mock_load.return_value = (_make_cloud_cfg(), tmp_path)
        ssh = _make_ssh_mock(stderr="command exited early with status 7\n", success=False)
        mock_ssh_maker.return_value = ssh

        result = CliRunner().invoke(cli, ["remote", "sync", "my_source"])

        assert result.exit_code == 1
        assert "command exited early with status 7" in _ANSI_RE.sub("", result.output)
        assert "Sync triggered" not in result.output
        ssh.disconnect.assert_called_once()

    @patch(f"{_PATCH_MGMT}._make_ssh_manager")
    @patch(f"{_PATCH_MGMT}._load_cloud_config_with_ip")
    def test_no_wait_started_output_is_success(self, mock_load, mock_ssh_maker, tmp_path):
        mock_load.return_value = (_make_cloud_cfg(), tmp_path)
        mock_ssh_maker.return_value = _make_ssh_mock(stdout="started pid=4242\n")

        result = CliRunner().invoke(cli, ["remote", "sync", "my_source"])

        assert result.exit_code == 0
        assert "Sync triggered" in result.output

    @patch(f"{_PATCH_MGMT}._make_ssh_manager")
    @patch(f"{_PATCH_MGMT}._load_cloud_config_with_ip")
    def test_cli_cloud_ssh_error_is_reported(self, mock_load, mock_ssh_maker, tmp_path):
        mock_load.return_value = (_make_cloud_cfg(), tmp_path)
        ssh = _make_ssh_mock()
        ssh.exec_command.side_effect = CloudSSHError("channel timed out")
        mock_ssh_maker.return_value = ssh

        result = CliRunner().invoke(cli, ["remote", "sync", "my_source"])

        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)  # not a raw CloudSSHError traceback
        assert "channel timed out" in result.output
        ssh.disconnect.assert_called_once()

    @patch(f"{_PATCH_MGMT}._make_ssh_manager")
    @patch(f"{_PATCH_MGMT}._load_cloud_config_with_ip")
    def test_no_wait_failed_exec_default_message(self, mock_load, mock_ssh_maker, tmp_path):
        mock_load.return_value = (_make_cloud_cfg(), tmp_path)
        ssh = _make_ssh_mock(stderr="", success=False)
        mock_ssh_maker.return_value = ssh

        result = CliRunner().invoke(cli, ["remote", "sync", "my_source"])

        assert result.exit_code == 1
        assert "Could not start sync" in result.output

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
        # The command looks like: sh -c '...nohup sudo -u dango ... '{...}' > ...
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
