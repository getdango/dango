"""tests/unit/test_remote_root_reads_service_user.py

Root SSH steps that read or delete project files, or run project Python, go
through the ``dango`` service user: from ``/``, isolated (``-I``), and never as
root on a project path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands import remote_backup
from dango.cli.commands.deploy import _handle_reconnect
from dango.cli.commands.remote_backup import backup_enable
from dango.cli.commands.remote_repair import _USER_RESYNC_SCRIPT, remote_reset_metabase
from dango.cli.commands.remote_sync import remote_sync
from dango.platform.cloud._server_templates import build_systemd_unit
from dango.platform.cloud.ssh import CommandResult

_PROJECT = "/srv/dango/project"
_ISOLATED_PY = "/srv/dango/venv/bin/python3 -I"
_ROOT_PROJECT_TOOLS = re.compile(r"(?<![\w-])(cat|grep|rm|head)\b[^|;&]*/srv/dango/project")


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", exit_code=0)


def _patch_cloud(ssh: MagicMock, tmp_path: Path):
    return (
        patch(
            "dango.cli.commands.remote_mgmt._load_cloud_config_with_ip",
            return_value=(MagicMock(droplet_ip="203.0.113.1"), tmp_path),
        ),
        patch("dango.cli.commands.remote_mgmt._make_ssh_manager", return_value=ssh),
    )


def _assert_no_root_project_tool(command: str) -> None:
    """Every project-path ``cat/grep/rm`` must sit behind ``runuser -u dango``."""
    for part in re.split(r"[;&]|\|\|", command):
        if _ROOT_PROJECT_TOOLS.search(part):
            assert "runuser -u dango --" in part, command


class TestResetMetabase:
    def _run(self, tmp_path: Path) -> list[str]:
        ssh = MagicMock()
        ssh.exec_command.side_effect = lambda c, *a, **k: _ok("a" * 32 if "print(" in c else "ok")
        p1, p2 = _patch_cloud(ssh, tmp_path)
        with (
            p1,
            p2,
            patch("time.sleep"),
            patch(
                "dango.platform.cloud.backup.get_remote_compose_project_name",
                return_value="dango-x",
            ),
        ):
            result = CliRunner().invoke(remote_reset_metabase, input="reset\n")
        assert result.exit_code == 0, result.output
        return [c.args[0] for c in ssh.exec_command.call_args_list]

    def test_metabase_yml_removed_as_service_user(self, tmp_path: Path) -> None:
        commands = self._run(tmp_path)
        rm = [c for c in commands if "metabase.yml" in c]
        assert len(rm) == 1
        assert rm[0].startswith("cd / && runuser -u dango -- rm -f "), rm[0]
        for c in commands:
            _assert_no_root_project_tool(c)

    def test_user_resync_isolated_from_root_dir(self, tmp_path: Path) -> None:
        commands = self._run(tmp_path)
        resync = [c for c in commands if "metabase_user_id" in c]
        assert len(resync) == 1
        cmd = resync[0]
        assert cmd.startswith("cd / && runuser -u dango -- env DANGO_CLOUD_MODE=true "), cmd
        assert f" {_ISOLATED_PY} -c " in cmd
        assert cmd.endswith(f" {_PROJECT}"), cmd
        assert "sudo" not in cmd
        assert f"cd {_PROJECT}" not in cmd

    def test_resync_script_takes_project_dir_from_argv(self) -> None:
        assert "sys.argv[1]" in _USER_RESYNC_SCRIPT
        assert 'Path(".")' not in _USER_RESYNC_SCRIPT


class TestBackupEnable:
    def _run(self, tmp_path: Path, env_content: str) -> tuple[object, list[str]]:
        ssh = MagicMock()

        def fake(command: str, *args: object, **kwargs: object) -> CommandResult:
            if "exec cat" in command:
                return _ok(env_content)
            return _ok("1.0.14")

        ssh.exec_command.side_effect = fake
        with patch("dango.cli.utils.load_cloud_config_with_ssh", return_value=(MagicMock(), ssh)):
            result = CliRunner().invoke(backup_enable)
        return result, [c.args[0] for c in ssh.exec_command.call_args_list]

    def test_key_present_is_read_as_service_user(self, tmp_path: Path) -> None:
        result, commands = self._run(tmp_path, "FOO=1\nSPACES_ACCESS_KEY=abc\n")
        assert result.exit_code == 0, result.output
        assert not any("grep -q SPACES_ACCESS_KEY" in c for c in commands)
        reads = [c for c in commands if f"{_PROJECT}/.env" in c]
        assert reads and all("runuser -u dango --" in c for c in reads)

    def test_key_missing_stops(self, tmp_path: Path) -> None:
        result, commands = self._run(tmp_path, "FOO=1\n# SPACES_ACCESS_KEY=old\n")
        assert result.exit_code == 1
        assert "SPACES_ACCESS_KEY not found" in result.output
        assert len(commands) == 1

    def test_unused_root_venv_constant_removed(self) -> None:
        assert not hasattr(remote_backup, "VENV_PYTHON")


class TestReconnect:
    def _run(self, tmp_path: Path, project_yml: str) -> list[str]:
        (tmp_path / ".dango").mkdir()
        (tmp_path / ".dango" / "cloud_key").write_text("k")
        ssh = MagicMock()
        ssh.exec_command.return_value = _ok(project_yml)
        with patch("dango.platform.cloud.ssh.SSHManager", return_value=ssh):
            try:
                _handle_reconnect(tmp_path, "203.0.113.1")
            finally:
                commands = [c.args[0] for c in ssh.exec_command.call_args_list]
        return commands

    def test_project_yml_read_as_service_user(self, tmp_path: Path) -> None:
        commands = self._run(tmp_path, "name: x\n")
        assert commands
        for c in commands:
            assert "runuser -u dango --" in c
            assert "head" not in c
        assert (tmp_path / ".dango" / "cloud.yml").exists()

    def test_missing_project_yml_is_not_a_deployment(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            self._run(tmp_path, "")


class TestRemoteSync:
    def _cmds(self, tmp_path: Path, *args: str) -> list[str]:
        ssh = MagicMock()
        ssh.exec_command.return_value = _ok(json.dumps({"status": "success"}))
        p1, p2 = _patch_cloud(ssh, tmp_path)
        with p1, p2:
            result = CliRunner().invoke(remote_sync, ["src", *args])
        assert result.exit_code == 0, result.output
        return [c.args[0] for c in ssh.exec_command.call_args_list]

    @pytest.mark.parametrize("flags", [["--wait"], []])
    def test_sync_isolated_as_service_user_from_root_dir(
        self, tmp_path: Path, flags: list[str]
    ) -> None:
        cmd = self._cmds(tmp_path, *flags)[0]
        assert "runuser -u dango -- env DANGO_CLOUD_MODE=true " in cmd
        assert f" {_ISOLATED_PY} -m dango.platform.scheduling.sync_trigger " in cmd
        assert "sudo" not in cmd
        assert f"cd {_PROJECT}" not in cmd
        if flags:
            assert cmd.startswith("cd / && ")
        else:
            assert "cd / ||" in cmd


def _unit_env() -> dict[str, str]:
    """Environment the dango.service unit gives the web and scheduled syncs."""
    values: dict[str, str] = {}
    for line in build_systemd_unit().splitlines():
        if line.startswith("Environment="):
            key, _, value = line[len("Environment=") :].partition("=")
            values[key] = value
        if line.startswith("WorkingDirectory="):
            values["DANGO_PROJECT_ROOT"] = line.partition("=")[2]
    return values


def _assert_service_env(command: str) -> None:
    unit = _unit_env()
    for key in ("DLT_DATA_DIR", "DANGO_PROJECT_ROOT", "DANGO_CLOUD_MODE"):
        assert f" {key}={unit[key]} " in command, (key, command)


class TestServiceEnvironment:
    @pytest.mark.parametrize("flags", [["--wait"], []])
    def test_cli_sync_carries_unit_environment(self, tmp_path: Path, flags: list[str]) -> None:
        _assert_service_env(TestRemoteSync()._cmds(tmp_path, *flags)[0])

    def test_reset_resync_carries_unit_environment(self, tmp_path: Path) -> None:
        commands = TestResetMetabase()._run(tmp_path)
        _assert_service_env([c for c in commands if "metabase_user_id" in c][0])


class TestBackupEnableEdges:
    def _run(self, env_text: str) -> Any:
        ssh = MagicMock()
        ssh.exec_command.side_effect = lambda c, *a, **k: _ok(
            env_text if "exec cat" in c else "1.0.14"
        )
        with patch("dango.cli.utils.load_cloud_config_with_ssh", return_value=(MagicMock(), ssh)):
            return CliRunner().invoke(backup_enable)

    @pytest.mark.parametrize(
        ("env_text", "ok"),
        [
            ("SPACES_ACCESS_KEY = x\n", True),
            ("SPACES_ACCESS_KEY='x'\n", True),
            ("SPACES_ACCESS_KEY=\n", False),
            ("SPACES_ACCESS_KEY_OLD=x\n", False),
        ],
    )
    def test_key_check_matches_env_parser(self, env_text: str, ok: bool) -> None:
        result = self._run(env_text)
        assert (result.exit_code == 0) is ok, result.output

    def test_unreadable_env_is_a_clean_error(self) -> None:
        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="denied", exit_code=1)
        with patch("dango.cli.utils.load_cloud_config_with_ssh", return_value=(MagicMock(), ssh)):
            result = CliRunner().invoke(backup_enable)
        assert result.exit_code == 1
        assert "Error" in result.output and "Traceback" not in result.output
        assert isinstance(result.exception, SystemExit)


def test_reconnect_unreadable_project_file_is_a_clean_error(tmp_path: Path) -> None:
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "cloud_key").write_text("k")
    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(stdout="", stderr="denied", exit_code=1)
    with patch("dango.platform.cloud.ssh.SSHManager", return_value=ssh):
        with pytest.raises(SystemExit) as exc:
            _handle_reconnect(tmp_path, "203.0.113.1")
    assert exc.value.code == 1
    ssh.disconnect.assert_called_once()
