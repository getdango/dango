"""tests/unit/test_remote_commands_service_user.py

Server-side ``remote`` commands run Dango code and touch project files as the
``dango`` service user, from ``/`` and isolated (``-I``), never as root in the
project directory.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote_auth import auth_group
from dango.cli.commands.remote_env import env
from dango.cli.commands.remote_mgmt import remote_query
from dango.cli.commands.remote_repair import (
    _metabase_schema_scan_command,
    _metabase_schema_scan_script,
    _metabase_secret_project_id_command,
)
from dango.platform.cloud.ssh import CommandResult

_AS_DANGO_RE = re.compile(r"runuser -u dango -- ")


def _assert_service_user_from_root_dir(command: str) -> None:
    assert command.startswith("cd / && "), command
    assert _AS_DANGO_RE.search(command), command
    assert "cd /srv/dango/project &&" not in command.split("runuser", 1)[0], command


def _ssh(stdout: str = "ok") -> MagicMock:
    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(stdout=stdout, stderr="", exit_code=0)
    return ssh


class TestRemoteQuery:
    def test_runs_isolated_as_service_user(self, tmp_path: Path) -> None:
        ssh = _ssh('{"columns": [], "rows": []}')
        with (
            patch(
                "dango.cli.commands.remote_mgmt._load_cloud_config_with_ip",
                return_value=(MagicMock(droplet_ip="203.0.113.1"), tmp_path),
            ),
            patch("dango.cli.commands.remote_mgmt._make_ssh_manager", return_value=ssh),
        ):
            result = CliRunner().invoke(remote_query, ["SELECT 'a b'; --", "--timeout", "7"])
        assert result.exit_code == 0, result.output
        cmd = ssh.exec_command.call_args[0][0]
        _assert_service_user_from_root_dir(cmd)
        assert "runuser -u dango -- /srv/dango/venv/bin/python3 -I -c " in cmd
        assert cmd.endswith(f" {shlex.quote(chr(83) + 'ELECT ' + repr('a b') + '; --')} 7")


class TestRemoteAuth:
    @pytest.mark.parametrize(
        ("argv", "expect"),
        [
            (["list-users"], "auth list-users"),
            (["add-user", "a+b@co.com", "--role", "editor"], "auth add-user a+b@co.com"),
            (["reset-password", "a@co.com"], "auth reset-password a@co.com"),
            (["remove-user", "a@co.com"], "auth delete-user a@co.com"),
        ],
    )
    def test_cli_runs_as_service_user(self, tmp_path: Path, argv: list[str], expect: str) -> None:
        ssh = _ssh()
        with (
            patch(
                "dango.cli.commands.remote_mgmt._load_cloud_config_with_ip",
                return_value=(MagicMock(droplet_ip="203.0.113.1"), tmp_path),
            ),
            patch("dango.cli.commands.remote_mgmt._make_ssh_manager", return_value=ssh),
        ):
            result = CliRunner().invoke(auth_group, argv)
        assert result.exit_code == 0, result.output
        cmd = ssh.exec_command.call_args[0][0]
        assert cmd.startswith("cd / && "), cmd
        assert "runuser -u dango -- sh -c " in cmd
        # The project directory is entered by the service user, inside the inner shell.
        inner = shlex.split(cmd.split("runuser -u dango -- sh -c ", 1)[1])[0]
        assert inner.startswith("cd /srv/dango/project && exec env DANGO_CLOUD_MODE=true ")
        assert expect in inner
        assert "cd /srv/dango/project" not in cmd.split("runuser", 1)[0]
        if argv[0] == "remove-user":
            assert "yes | runuser" in cmd


class TestRemoteRepair:
    def test_project_id_lookup_as_service_user(self) -> None:
        cmd = _metabase_secret_project_id_command("/srv/dango/project")
        _assert_service_user_from_root_dir(cmd)
        assert "runuser -u dango -- /srv/dango/venv/bin/python -I -c " in cmd

    def test_schema_scan_as_service_user_in_cloud_mode(self) -> None:
        cmd = _metabase_schema_scan_command("/srv/dango/project")
        _assert_service_user_from_root_dir(cmd)
        assert (
            "runuser -u dango -- env DANGO_CLOUD_MODE=true /srv/dango/venv/bin/python -I -c " in cmd
        )
        assert cmd.endswith(" /srv/dango/project")

    @pytest.mark.parametrize(
        ("database_id", "expect_sync"),
        [(7, True), ("7/../../admin", False), (True, False), (0, False), (None, False)],
    )
    def test_database_id_must_be_a_positive_int(
        self, tmp_path: Path, database_id: object, expect_sync: bool
    ) -> None:
        """Run the generated script against stub modules and record the HTTP calls."""
        harness = tmp_path / "harness.py"
        harness.write_text(
            "import sys, types\n"
            "calls = []\n"
            "cfg = types.ModuleType('dango.security.metabase_config')\n"
            f"cfg.load_metabase_metadata = lambda root: {{'database': {{'id': {database_id!r}}}}}\n"
            "cfg.load_metabase_admin_credentials = lambda root: ('a@b.c', 'pw')\n"
            "for name in ('dango', 'dango.security'):\n"
            "    sys.modules[name] = types.ModuleType(name)\n"
            "sys.modules['dango.security.metabase_config'] = cfg\n"
            "class R:\n"
            "    status_code = 200\n"
            "    def json(self): return {'id': 'sess'}\n"
            "req = types.ModuleType('requests')\n"
            "def post(url, **kw):\n"
            "    calls.append(url); return R()\n"
            "req.post = post\n"
            "sys.modules['requests'] = req\n"
            "sys.argv = ['x', '/srv/dango/project']\n"
            "try:\n"
            f"    exec({_metabase_schema_scan_script()!r})\n"
            "except SystemExit:\n"
            "    pass\n"
            "print(calls)\n"
        )
        out = subprocess.run(
            [sys.executable, "-I", str(harness)], capture_output=True, text=True, check=True
        ).stdout
        if expect_sync:
            assert "/api/database/7/sync_schema" in out
        else:
            assert "sync_schema" not in out


class TestRemoteEnv:
    def test_env_file_is_read_and_written_as_service_user(self) -> None:
        ssh = _ssh("FOO=old\n")
        with patch(
            "dango.cli.commands.remote._ssh_connect_or_fail",
            return_value=(MagicMock(), ssh, Path("/p")),
        ):
            result = CliRunner().invoke(env, ["set", "BAR=1"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        commands = [c.args[0] for c in ssh.exec_command.call_args_list]
        assert commands and all("runuser -u dango --" in c for c in commands)
        # Root never reads or writes the dango-owned path directly.
        ssh.read_remote_file.assert_not_called()
        for call in ssh.write_remote_file.call_args_list:
            assert call.args[0].startswith("/root/.dango-upload-")

    def test_unreadable_env_does_not_overwrite(self) -> None:
        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="denied", exit_code=1)
        with patch(
            "dango.cli.commands.remote._ssh_connect_or_fail",
            return_value=(MagicMock(), ssh, Path("/p")),
        ):
            result = CliRunner().invoke(env, ["set", "BAR=1"])
        assert result.exit_code == 1
        ssh.write_remote_file.assert_not_called()
