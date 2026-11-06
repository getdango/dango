"""tests/unit/test_remote_migrate_off.py

`dango remote migrate` is switched off in 1.0.14: it refuses before any network call.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote import remote


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\x1b\[[^m]*m", "", text))


def _invoke(args: list[str], tmp_path: Path) -> tuple[Any, dict[str, MagicMock]]:
    """Run `remote <args>` with every network/SSH/config entry point replaced by a mock."""
    mocks = {
        "client": MagicMock(name="DigitalOceanClient"),
        "make_client": MagicMock(name="_make_client"),
        "ssh": MagicMock(name="SSHManager"),
        "connect_ssh": MagicMock(name="_connect_ssh"),
        "loader": MagicMock(name="ConfigLoader"),
        "migrate_server": MagicMock(name="migrate_server"),
    }
    with (
        patch("dango.platform.cloud.digitalocean.DigitalOceanClient", mocks["client"]),
        patch("dango.cli.commands.remote._make_client", mocks["make_client"]),
        patch("dango.platform.cloud.ssh.SSHManager", mocks["ssh"]),
        patch("dango.cli.commands.remote._connect_ssh", mocks["connect_ssh"]),
        patch("dango.config.loader.ConfigLoader", mocks["loader"]),
        patch("dango.platform.cloud.migrate.migrate_server", mocks["migrate_server"]),
    ):
        result = CliRunner().invoke(remote, args, obj={"project_root": tmp_path})
    return result, mocks


@pytest.mark.unit
class TestMigrateRefuses:
    @pytest.mark.parametrize(
        "args",
        [
            ["migrate"],
            ["migrate", "--size", "s-4vcpu-8gb"],
            ["migrate", "--size", "s-4vcpu-8gb", "--region", "sfo3", "--yes"],
            ["migrate", "--yes"],
        ],
    )
    def test_refuses_with_exit_1_and_no_network_call(self, tmp_path: Path, args: list[str]) -> None:
        result, mocks = _invoke(args, tmp_path)

        assert result.exit_code == 1, result.output
        assert "switched off in 1.0.14" in _plain(result.output)
        for name, mock in mocks.items():
            assert not mock.called, f"{name} was called"

    def test_message_names_resize_and_the_four_manual_steps(self, tmp_path: Path) -> None:
        result, _ = _invoke(["migrate", "--size", "s-4vcpu-8gb"], tmp_path)

        text = _plain(result.output)
        assert "switched off in 1.0.14" in text
        assert "dango remote resize" in text
        steps = [
            "(1) dango remote backup download --from-server -o ./backup.tar.gz",
            "(2) dango deploy destroy",
            "(3) dango deploy",
            "(4) dango remote backup restore ./backup.tar.gz --from-local",
        ]
        for step in steps:
            assert step in text
        assert text.index(steps[0]) < text.index(steps[1]) < text.index(steps[2])
        assert text.index(steps[2]) < text.index(steps[3])
        assert "re-enter secrets" in text
        assert "update DNS" in text

    def test_command_stays_registered_and_help_says_switched_off(self, tmp_path: Path) -> None:
        assert "migrate" in remote.commands
        result = CliRunner().invoke(remote, ["migrate", "--help"])
        assert result.exit_code == 0
        assert "switched off in 1.0.14" in _plain(result.output)
