"""tests/unit/test_g4f1_root_project_paths.py

I8-03: root never appends to or reads a project path through a link the ``dango`` user planted.

The deployment journal, the deploy timestamp and the migrate secret files all live under
``/srv/dango/project``, which the service user owns.  Every one of these operations must run
as ``dango`` (``runuser -u dango --``): a command run directly over the root SSH connection
would follow a link planted at the file or at any parent directory.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from dango.platform.cloud.deploy_journal import (
    DeploymentRecord,
    get_latest_deployment,
    read_remote_journal,
    write_remote_journal,
)
from dango.platform.cloud.ssh import CommandResult

_PROJECT = "/srv/dango/project"
_AS_DANGO = "runuser -u dango --"
_SHIM = '#!/bin/sh\n[ "$1" = -u ] && shift 2\n[ "$1" = -- ] && shift\nexec "$@"\n'


class PlantedLinkSSH:
    """Fake root SSH connection that classifies every command it receives.

    A command that does not start with ``runuser -u dango --`` runs with root's reach, so
    one that names a project path would follow a link planted there.  Commands that do start
    with it are executed locally (against a sandbox project) through a ``runuser`` shim.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.project = tmp_path / "project"
        self.project.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "runuser"
        shim.write_text(_SHIM)
        shim.chmod(0o755)
        self.path = f"{bin_dir}:/usr/bin:/bin"
        self.root_commands: list[str] = []
        self.dango_commands: list[str] = []

    def exec_command(self, command: str, timeout: int | None = None, **_: object) -> CommandResult:
        if not command.startswith(_AS_DANGO):
            self.root_commands.append(command)
            return CommandResult(stdout="", stderr="", exit_code=0)
        self.dango_commands.append(command)
        proc = subprocess.run(
            ["sh", "-c", command.replace(_PROJECT, str(self.project))],
            capture_output=True,
            text=True,
            env={"PATH": self.path},
            check=False,
        )
        return CommandResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)

    def root_touched(self, needle: str) -> list[str]:
        return [c for c in self.root_commands if needle in c]


def _record(**kwargs: Any) -> DeploymentRecord:
    fields: dict[str, Any] = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "deployer": "alice",
        "success": True,
    }
    fields.update(kwargs)
    return DeploymentRecord(**fields)


@pytest.mark.unit
class TestJournalAppendRunsAsDango:
    def test_append_never_runs_as_root(self, tmp_path: Path) -> None:
        ssh = PlantedLinkSSH(tmp_path)
        write_remote_journal(ssh, _record())
        assert ssh.root_touched("deployments.jsonl") == []
        assert ssh.root_touched(".dango/state") == []
        assert len(ssh.dango_commands) == 1

    def test_append_works_and_keeps_hostile_input_verbatim(self, tmp_path: Path) -> None:
        ssh = PlantedLinkSSH(tmp_path)
        nasty = "x'; touch /tmp/pwned; echo '\"$(id)\" `id` \\n end"
        write_remote_journal(ssh, _record(error=nasty))
        write_remote_journal(ssh, _record(deployer="bob"))
        journal = ssh.project / ".dango" / "state" / "deployments.jsonl"
        lines = journal.read_text().splitlines()
        assert [json.loads(line)["deployer"] for line in lines] == ["alice", "bob"]
        assert json.loads(lines[0])["error"] == nasty
        assert not Path("/tmp/pwned").exists()

    def test_planted_final_link_is_never_followed_by_root(self, tmp_path: Path) -> None:
        ssh = PlantedLinkSSH(tmp_path)
        state = ssh.project / ".dango" / "state"
        state.mkdir(parents=True)
        (state / "deployments.jsonl").symlink_to(tmp_path / "root-only-victim")
        write_remote_journal(ssh, _record())
        assert ssh.root_touched("deployments.jsonl") == []

    def test_planted_parent_link_is_never_followed_by_root(self, tmp_path: Path) -> None:
        ssh = PlantedLinkSSH(tmp_path)
        (tmp_path / "victim-dir").mkdir()
        (ssh.project / ".dango").symlink_to(tmp_path / "victim-dir")
        write_remote_journal(ssh, _record())
        assert ssh.root_touched(".dango") == []
        assert ssh.root_touched("mkdir") == []


@pytest.mark.unit
class TestJournalReadsRunAsDango:
    def test_read_journal_as_dango(self) -> None:
        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(
            stdout=json.dumps({"deployer": "a"}) + "\n", stderr="", exit_code=0
        )
        assert read_remote_journal(ssh, limit=5)[0]["deployer"] == "a"
        assert ssh.exec_command.call_args[0][0].startswith(_AS_DANGO)

    def test_latest_deployment_as_dango(self) -> None:
        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(
            stdout=json.dumps({"deployer": "a"}) + "\n", stderr="", exit_code=0
        )
        assert get_latest_deployment(ssh) == {"deployer": "a"}
        assert ssh.exec_command.call_args[0][0].startswith(_AS_DANGO)


@pytest.mark.unit
class TestOtherRootReadsOfProjectFiles:
    def test_deploy_timestamp_read_as_dango(self) -> None:
        from dango.platform.cloud.server_status import _get_last_deploy

        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(stdout="2026-01-01", stderr="", exit_code=0)
        assert _get_last_deploy(ssh) == "2026-01-01"
        assert ssh.exec_command.call_args[0][0].startswith(_AS_DANGO)

    def test_migrate_secret_files_read_as_dango(self) -> None:
        from dango.platform.cloud.migrate import _copy_secrets_between_servers

        old_ssh = MagicMock()
        old_ssh.exec_command.return_value = CommandResult(stdout="K=v", stderr="", exit_code=0)
        new_ssh = MagicMock()
        new_ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
        assert _copy_secrets_between_servers(old_ssh, new_ssh) == []
        reads = [c[0][0] for c in old_ssh.exec_command.call_args_list]
        assert reads and all(c.startswith(_AS_DANGO) for c in reads)
