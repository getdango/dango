"""tests/unit/test_remote_metabase_admin_command.py

Verify dango remote metabase-repair-admin sends the exact server command over a fake SSH
connection. It confirms first (unless --yes) and reports failures without a traceback.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from dango.cli.commands.remote import remote
from dango.exceptions import CloudSSHError
from dango.platform.cloud.ssh import CommandResult

_MGMT = "dango.cli.commands.remote_mgmt"
_COMMAND = (
    "cd /srv/dango/project && sudo -u dango -H env DANGO_CLOUD_MODE=true "
    "/srv/dango/venv/bin/dango metabase repair-admin"
)


def _invoke(
    args: list[str],
    ssh: MagicMock,
    *,
    input_text: str | None = None,
) -> Result:
    cloud_cfg = MagicMock()
    cloud_cfg.droplet_ip = "203.0.113.7"
    with (
        patch(f"{_MGMT}._load_cloud_config_with_ip", return_value=(cloud_cfg, Path("."))),
        patch(f"{_MGMT}._make_ssh_manager", return_value=ssh),
    ):
        return CliRunner().invoke(
            remote, ["metabase-repair-admin", *args], input=input_text, obj={}
        )


def _plain(output: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", output)


def _ssh(result: CommandResult) -> MagicMock:
    ssh = MagicMock()
    ssh.exec_command.return_value = result
    return ssh


@pytest.mark.unit
def test_sends_exact_command_with_timeout_and_skips_prompt_with_yes() -> None:
    ssh = _ssh(CommandResult("Metabase admin access restored.\n", "", 0))
    result = _invoke(["--yes"], ssh)

    assert result.exit_code == 0, result.output
    ssh.exec_command.assert_called_once_with(_COMMAND, timeout=900, check=False)
    ssh.connect.assert_called_once_with("203.0.113.7")
    ssh.disconnect.assert_called_once()
    assert "Metabase admin access restored." in result.output
    assert "(yes/no)" not in result.output


@pytest.mark.unit
def test_prompts_without_yes_and_declining_sends_nothing() -> None:
    ssh = _ssh(CommandResult("", "", 0))
    result = _invoke([], ssh, input_text="no\n")

    assert result.exit_code != 0
    assert "no Metabase data is changed" in result.output
    ssh.exec_command.assert_not_called()


@pytest.mark.unit
def test_confirming_runs_the_repair() -> None:
    ssh = _ssh(CommandResult("Metabase admin access restored.\n", "", 0))
    result = _invoke([], ssh, input_text="yes\n")

    assert result.exit_code == 0, result.output
    ssh.exec_command.assert_called_once()


@pytest.mark.unit
def test_nonzero_exit_aborts_with_server_reason() -> None:
    ssh = _ssh(CommandResult("Metabase is not reachable.\n", "", 1))
    result = _invoke(["-y"], ssh)

    assert result.exit_code == 1
    assert "Metabase is not reachable." in result.output
    assert "Metabase admin access restored." not in result.output
    ssh.disconnect.assert_called_once()


@pytest.mark.unit
def test_nonzero_exit_falls_back_to_stderr() -> None:
    ssh = _ssh(CommandResult("", "sudo: a password is required", 1))
    result = _invoke(["-y"], ssh)

    assert result.exit_code == 1
    assert "sudo: a password is required" in result.output


@pytest.mark.unit
def test_ssh_error_aborts_without_traceback() -> None:
    ssh = MagicMock()
    ssh.exec_command.side_effect = CloudSSHError("connection lost")
    result = _invoke(["-y"], ssh)

    assert result.exit_code == 1
    assert "connection lost" in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, CloudSSHError)
    ssh.exec_command.assert_called_once()


@pytest.mark.unit
def test_connect_failure_aborts_without_traceback() -> None:
    ssh = MagicMock()
    ssh.connect.side_effect = CloudSSHError("SSH authentication failed")
    result = _invoke(["-y"], ssh)

    assert result.exit_code == 1
    assert "SSH authentication failed" in result.output
    ssh.exec_command.assert_not_called()


@pytest.mark.unit
def test_server_output_brackets_are_not_read_as_markup() -> None:
    ssh = _ssh(CommandResult("banner\nrepaired [bold]x[/bold] [/nope]\n", "", 0))
    result = _invoke(["-y"], ssh)

    assert result.exit_code == 0, result.output
    assert "repaired [bold]x[/bold] [/nope]" in _plain(result.output)
    assert "banner" not in result.output


@pytest.mark.unit
def test_failure_prints_only_the_servers_one_line_reason() -> None:
    ssh = _ssh(CommandResult("Welcome banner\nMetabase [x] is not reachable.\n", "", 1))
    result = _invoke(["-y"], ssh)

    assert result.exit_code == 1
    assert "Metabase [x] is not reachable." in _plain(result.output)
    assert "Welcome banner" not in result.output


@pytest.mark.unit
def test_unexpected_connect_error_aborts_without_traceback() -> None:
    ssh = MagicMock()
    ssh.connect.side_effect = OSError("network down")
    result = _invoke(["-y"], ssh)

    assert result.exit_code == 1
    assert "network down" in result.output
    ssh.exec_command.assert_not_called()
