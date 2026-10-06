"""tests/unit/test_remote_backup_path_quoting.py

Unit tests (T12, D13/D14) that user-supplied and listed paths are shell-quoted
in remote backup/rollback commands, and that a local restore never uses the
local file name as the remote name.  A recording fake SSH is used; nothing is
executed, no host is contacted.  Unit-tested only, not verified on a live server.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote_backup import backup_restore
from dango.platform.cloud import backup
from dango.platform.cloud.ssh import CommandResult

HOSTILE_NAMES = [
    "My Backup (1).tar.gz",
    "it's.tar.gz",
    "a;b.tar.gz",
    "x$(id).tar.gz",
    "y`id`.tar.gz",
]
HOSTILE_REMOTE_PATHS = [
    "/srv/dango/backups/deploy/my backup.tar.gz",
    "/x; touch /tmp/pwned.tar.gz",
    "/srv/it's$(id)`id`.tar.gz",
]


class FakeSSH:
    """Records commands; answers success, optionally with canned stdout."""

    def __init__(self, ls_output: str = "") -> None:
        self.commands: list[str] = []
        self.uploads: list[tuple[Path, str]] = []
        self.ls_output = ls_output

    def exec_command(self, command: str, timeout: int | None = None, check: bool = False) -> Any:
        self.commands.append(command)
        stdout = self.ls_output if command.startswith("ls -1t") else ""
        return CommandResult(stdout=stdout, stderr="", exit_code=0)

    def upload_file(self, local_path: Path, remote_path: str, **_: Any) -> None:
        self.uploads.append((Path(local_path), remote_path))

    def disconnect(self) -> None:
        pass


def _unquoted_shell_chars(cmd: str) -> bool:
    """True if shlex sees a bare ``;`` / ``touch`` / ``id`` token (injection)."""
    tokens = shlex.split(cmd)
    return any(t in {";", "touch", "id"} for t in tokens)


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_restore_local_uses_generated_remote_name(name: str, tmp_path: Path) -> None:
    """P1/P2: the local file name never reaches the remote path or a shell command.

    The upload target and the restore/cleanup path are a generated
    /tmp/dango-restore-<32 hex>.tar.gz, whatever the local name looks like.
    """
    local = tmp_path / name
    local.write_bytes(b"x")
    ssh = FakeSSH()
    restored: list[str] = []

    def fake_restore(_ssh: Any, path: str, **_: Any) -> Any:
        restored.append(path)
        return type("R", (), {"health_check_passed": True, "warnings": []})()

    with (
        patch(
            "dango.cli.commands.remote_backup._load_cloud_config_with_ssh_or_fail",
            return_value=(object(), ssh),
        ),
        patch("dango.platform.cloud.backup.restore_from_archive", side_effect=fake_restore),
    ):
        result = CliRunner().invoke(backup_restore, [str(local), "--from-local", "--yes"])

    assert result.exit_code == 0, result.output
    pattern = re.compile(r"^/tmp/dango-restore-[0-9a-f]{32}\.tar\.gz$")
    assert len(ssh.uploads) == 1
    remote = ssh.uploads[0][1]
    assert pattern.match(remote)
    assert restored == [remote]
    assert ssh.commands == [f"rm -f {remote}"]
    assert all(name not in c for c in ssh.commands)


def _patched_restore(ssh: FakeSSH, path: str) -> None:
    with (
        patch.object(backup, "create_backup"),
        patch.object(backup, "stop_services"),
        patch.object(backup, "start_services"),
        patch.object(backup, "verify_health", return_value=True),
        patch.object(backup, "_get_metabase_volume_path", return_value="/vol/mb"),
        patch.object(backup, "_sanitize_remote_metabase_yaml", return_value=None),
    ):
        backup.rollback(ssh, backup_path=path)  # type: ignore[arg-type]


@pytest.mark.parametrize("path", HOSTILE_REMOTE_PATHS)
def test_rollback_quotes_backup_path(path: str) -> None:
    """P3/P4: rollback --backup <path> is one quoted token in every command.

    Covers test -f, cat manifest, tar extract, restore copies and cleanup;
    no command may contain an unquoted ``;`` or split the path.
    """
    ssh = FakeSSH()
    _patched_restore(ssh, path)

    base = path.rsplit("/", 1)[-1].replace(".tar.gz", "")
    staging = f"/tmp/{base}"
    assert len(ssh.commands) >= 5
    all_tokens: list[str] = []
    for cmd in ssh.commands:
        assert not _unquoted_shell_chars(cmd), cmd
        all_tokens.extend(shlex.split(cmd))
    assert path in all_tokens
    assert path.replace(".tar.gz", ".json") in all_tokens
    assert staging in all_tokens
    assert any(t.startswith(f"{staging}/") for t in all_tokens)
    tar_cmd = next(c for c in ssh.commands if "tar -xzf" in c)
    assert shlex.split(tar_cmd) == ["rm", "-rf", staging, "&&", "tar", "-xzf", path, "-C", "/tmp"]
    test_cmd = ssh.commands[0]
    assert shlex.split(test_cmd) == ["test", "-f", path]


def test_safe_path_commands_unchanged() -> None:
    """P5: a safe path quotes to itself, so commands keep their old text."""
    path = "/srv/dango/backups/deploy/backup-20260224-143000.tar.gz"
    ssh = FakeSSH()
    _patched_restore(ssh, path)
    assert f"test -f {path}" in ssh.commands
    assert f"cat {path.replace('.tar.gz', '.json')} 2>/dev/null" in ssh.commands
    assert f"rm -rf /tmp/backup-20260224-143000 && tar -xzf {path} -C /tmp" in ssh.commands


def test_cleanup_and_stat_quote_listed_names() -> None:
    """P6: stat and retention rm -f quote names listed from the backup directory.

    A listed name with a space or quote must stay a single token; safe names
    keep their previous command text.
    """
    bad = f"{backup.BACKUP_DIR}/odd name's.tar.gz"
    old = f"{backup.BACKUP_DIR}/backup-20200101-000000.tar.gz"
    new = f"{backup.BACKUP_DIR}/backup-20260101-000000.tar.gz"
    ssh = FakeSSH(ls_output="\n".join([new, bad, old]))

    listed = backup.list_local_backups(ssh)  # type: ignore[arg-type]
    assert [b["path"] for b in listed] == [new, bad, old]
    stat_cmd = next(c for c in ssh.commands if "odd" in c and c.startswith("stat"))
    assert shlex.split(stat_cmd)[-2:] == [bad, "2>/dev/null"] or bad in shlex.split(stat_cmd)

    ssh.commands.clear()
    assert backup.rotate_local_backups(ssh, keep=1) == 2  # type: ignore[arg-type]
    rm_cmds = [c for c in ssh.commands if c.startswith("rm -f")]
    assert shlex.split(rm_cmds[0]) == ["rm", "-f", bad, bad.replace(".tar.gz", ".json")]
    assert rm_cmds[1] == f"rm -f {old} {old.replace('.tar.gz', '.json')}"
