"""tests/unit/test_remote_reset_metabase_credentials.py

Credential handling of ``dango remote reset-metabase`` against a recording
fake SSH (no real host is ever contacted).  Covers the files removed, the
accepted project-id shapes, validation before any state change, the cloud-mode
re-sync wrapper, and reporting of a failed service start.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote_repair import remote_reset_metabase
from dango.platform.cloud.ssh import CommandResult

_ID32 = "ab12cd34" * 4
_ID8 = "ab12cd34"
_SECRETS = "/srv/dango/secrets/metabase"


class _FakeSSH:
    """Records every command in order; answers from a rule table."""

    def __init__(self, project_id_output: str, *, start_ok: bool = True) -> None:
        self.commands: list[str] = []
        self._id_output = project_id_output
        self._start_ok = start_ok

    def connect(self, _ip: str) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def exec_command(self, command: str, timeout: int = 0, **_kw: object) -> CommandResult:
        self.commands.append(command)
        rules: list[tuple[Callable[[str], bool], CommandResult]] = [
            (lambda c: "MetabaseCredentialStore" in c, CommandResult(self._id_output, "", 0)),
            (
                lambda c: c == "systemctl start dango-web",
                CommandResult("", "", 0 if self._start_ok else 1),
            ),
            (lambda c: "api/health" in c, CommandResult("ok\n", "", 0)),
            (lambda c: "sync_all_users_to_metabase" in c, CommandResult("Synced: 1\n", "", 0)),
        ]
        for matches, result in rules:
            if matches(command):
                return result
        return CommandResult("", "", 0)


def _invoke(project_id_output: str, *, start_ok: bool = True):
    ssh = _FakeSSH(project_id_output, start_ok=start_ok)
    cfg = MagicMock(droplet_ip="203.0.113.1")
    with (
        patch(
            "dango.cli.commands.remote_mgmt._load_cloud_config_with_ip",
            return_value=(cfg, Path("/project")),
        ),
        patch("dango.cli.commands.remote_mgmt._make_ssh_manager", return_value=ssh),
        patch(
            "dango.platform.cloud.backup.get_remote_compose_project_name",
            return_value="dango-ab12cd34",
        ),
        patch("time.sleep"),
    ):
        result = CliRunner().invoke(remote_reset_metabase, input="reset\n")
    return result, ssh.commands


@pytest.mark.unit
class TestResetMetabaseCredentials:
    """Credential-store handling of reset-metabase."""

    def test_reset_removes_both_store_files(self) -> None:
        result, commands = _invoke(_ID32 + "\n")
        assert result.exit_code == 0, result.output
        expected = f"rm -f {_SECRETS}/{_ID32}.json {_SECRETS}/{_ID32}.pending.json"
        assert expected in commands
        assert not any("rm -rf" in c for c in commands)
        assert commands.index(expected) < commands.index("systemctl start dango-web")

    def test_reset_accepts_32_hex_id(self) -> None:
        result, commands = _invoke(_ID32 + "\n")
        assert result.exit_code == 0, result.output
        assert "systemctl stop dango-web 2>/dev/null || true" in commands

    def test_reset_accepts_8_hex_id(self) -> None:
        result, commands = _invoke(_ID8 + "\n")
        assert result.exit_code == 0, result.output
        assert f"rm -f {_SECRETS}/{_ID8}.json {_SECRETS}/{_ID8}.pending.json" in commands

    @pytest.mark.parametrize(
        "output",
        [
            "x; rm -rf /\n",
            "AB12CD34\n",
            "ab12cd34a\n",
            "ab12cd34" * 3 + "\n",
            "\n",
            "",
            _ID8 + "\n\n x",
        ],
    )
    def test_reset_rejects_bad_id_before_stopping_service(self, output: str) -> None:
        result, commands = _invoke(output)
        assert result.exit_code != 0
        assert "Nothing was stopped or removed" in result.output
        assert not any("systemctl" in c for c in commands)
        assert not any(c.startswith("rm ") or "docker compose" in c for c in commands)

    def test_reset_resync_runs_as_dango_in_cloud_mode(self) -> None:
        result, commands = _invoke(_ID32 + "\n")
        assert result.exit_code == 0, result.output
        resync = [c for c in commands if "sync_all_users_to_metabase" in c]
        assert len(resync) == 1
        assert (
            "sudo -u dango -H env DANGO_CLOUD_MODE=true /srv/dango/venv/bin/python3 -c"
            in (resync[0])
        )
        tokens = shlex.split(resync[0].split("&& ", 1)[1])
        assert tokens[:6] == ["sudo", "-u", "dango", "-H", "env", "DANGO_CLOUD_MODE=true"]
        compile(tokens[-1], "<resync>", "exec")  # the quoted script is one valid argument

    def test_reset_reports_failed_service_start(self) -> None:
        result, commands = _invoke(_ID32 + "\n", start_ok=False)
        assert result.exit_code != 0
        assert "Could not start dango-web" in result.output
        flat = " ".join(result.output.split())
        assert "half-applied" in flat
        assert "credential files are already removed" in flat
        assert "user re-sync did not run" in flat
        assert "dango remote reset-metabase" in flat
        assert not any("sync_all_users_to_metabase" in c for c in commands)
