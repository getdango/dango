"""tests/unit/test_remote_backup_path_quoting.py

Unit tests (T12, D13/D14) that user-supplied and listed paths are shell-quoted
in remote backup/rollback commands, and that a local restore never uses the
local file name as the remote name.  A recording fake SSH is used; nothing is
executed, no host is contacted.  Unit-tested only, not verified on a live server.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote_backup import backup_restore
from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import backup
from dango.platform.cloud.ssh import CommandResult, SSHManager

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


def _as_ssh(fake: FakeSSH) -> SSHManager:
    """View the recording fake as an SSHManager for the type checker."""
    return cast("SSHManager", fake)


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
    _patched_restore_with(ssh, path)


def _patched_restore_with(ssh: FakeSSH, path: str) -> None:
    with (
        patch.object(backup, "create_backup"),
        patch.object(backup, "stop_services"),
        patch.object(backup, "start_services"),
        patch.object(backup, "verify_health", return_value=True),
        patch.object(backup, "_get_metabase_volume_path", return_value="/vol/mb"),
        patch.object(backup, "_sanitize_remote_metabase_yaml", return_value=None),
    ):
        backup.rollback(_as_ssh(ssh), backup_path=path)


@pytest.mark.parametrize("path", HOSTILE_REMOTE_PATHS)
def test_rollback_quotes_backup_path(path: str) -> None:
    """P3/P4: rollback --backup <path> is one quoted token in every command.

    Covers test -f, manifest cat, tar extract, restore copies and cleanup.
    No command may contain an unquoted ``;`` or split the path.
    """
    ssh = FakeSSH()
    _patched_restore(ssh, path)

    tar_cmd = next(c for c in ssh.commands if "tar -xzf" in c)
    staging = shlex.split(tar_cmd)[2]
    assert re.fullmatch(r"/tmp/dango-restore-staging-[0-9a-f]{32}", staging)
    assert len(ssh.commands) >= 5
    all_tokens: list[str] = []
    for cmd in ssh.commands:
        assert not _unquoted_shell_chars(cmd), cmd
        all_tokens.extend(shlex.split(cmd))
    assert path in all_tokens
    assert path.replace(".tar.gz", ".json") in all_tokens
    assert any(t.startswith(f"{staging}/") for t in all_tokens)
    assert shlex.split(tar_cmd) == [
        "mkdir",
        "-p",
        staging,
        "&&",
        "tar",
        "-xzf",
        path,
        "-C",
        staging,
        "--strip-components=1",
    ]
    assert f"rm -rf {shlex.quote(staging)}" in ssh.commands
    test_cmd = ssh.commands[0]
    assert shlex.split(test_cmd) == ["test", "-f", path]


def test_safe_path_commands_unchanged() -> None:
    """P5: a safe path quotes to itself, so commands keep their old text."""
    path = "/srv/dango/backups/deploy/backup-20260224-143000.tar.gz"
    ssh = FakeSSH()
    _patched_restore(ssh, path)
    assert f"test -f {path}" in ssh.commands
    assert f"cat {path.replace('.tar.gz', '.json')} 2>/dev/null" in ssh.commands
    tar_cmd = next(c for c in ssh.commands if "tar -xzf" in c)
    assert f"tar -xzf {path} -C /tmp/dango-restore-staging-" in tar_cmd


def test_cleanup_and_stat_quote_listed_names() -> None:
    """P6: stat and retention rm -f quote names listed from the backup directory.

    A listed name with a space or quote must stay a single token; safe names
    keep their previous command text.
    """
    bad = f"{backup.BACKUP_DIR}/odd name's.tar.gz"
    old = f"{backup.BACKUP_DIR}/backup-20200101-000000.tar.gz"
    new = f"{backup.BACKUP_DIR}/backup-20260101-000000.tar.gz"
    ssh = FakeSSH(ls_output="\n".join([new, bad, old]))

    listed = backup.list_local_backups(_as_ssh(ssh))
    assert [b["path"] for b in listed] == [new, bad, old]
    stat_cmd = next(c for c in ssh.commands if "odd" in c and c.startswith("stat"))
    assert shlex.split(stat_cmd)[-2:] == [bad, "2>/dev/null"] or bad in shlex.split(stat_cmd)

    ssh.commands.clear()
    assert backup.rotate_local_backups(_as_ssh(ssh), keep=1) == 2
    rm_cmds = [c for c in ssh.commands if c.startswith("rm -f")]
    assert shlex.split(rm_cmds[0]) == ["rm", "-f", bad, bad.replace(".tar.gz", ".json")]
    assert rm_cmds[1] == f"rm -f {old} {old.replace('.tar.gz', '.json')}"


class ExecutingSSH(FakeSSH):
    """Really runs file-reading/copying commands under sh; records the rest.

    ``rm``, ``chown`` and ``ls`` are recorded but never executed.
    """

    def exec_command(self, command: str, timeout: int | None = None, check: bool = False) -> Any:
        self.commands.append(command)
        if command.startswith(("rm ", "chown ", "ls ")):
            return CommandResult(stdout="", stderr="", exit_code=0)
        proc = subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=False)
        return CommandResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)


@pytest.mark.parametrize(
    "archive_name",
    ["backup-20260224-143000.tar.gz", f"dango-restore-{'a1' * 16}.tar.gz"],
)
def test_restore_extracts_create_archive_layout_whatever_the_file_name(
    archive_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real tar: an archive in the _create_archive layout restores under any file name.

    The top-level dir (backup-<ts>) differs from the file name for uploaded
    archives; files must still land in PROJECT_DIR and only the generated
    staging dir is cleaned up.
    """
    top = tmp_path / "build" / "backup-20260224-143000"
    (top / "data").mkdir(parents=True)
    (top / "data" / "warehouse.duckdb").write_bytes(b"db")
    (top / "dbt" / "models").mkdir(parents=True)
    (top / "dbt" / "models" / "m.sql").write_text("select 1")
    archive = tmp_path / archive_name
    subprocess.run(
        ["tar", "-czf", str(archive), "-C", str(tmp_path / "build"), top.name], check=True
    )
    project = tmp_path / "project"
    stage_root = tmp_path / "stage"
    stage_root.mkdir()
    monkeypatch.setattr(backup, "PROJECT_DIR", str(project))
    monkeypatch.setattr(backup, "RESTORE_STAGING_ROOT", str(stage_root))
    ssh = ExecutingSSH()

    _patched_restore_with(ssh, str(archive))

    assert (project / "data" / "warehouse.duckdb").read_bytes() == b"db"
    assert (project / "dbt" / "models" / "m.sql").read_text() == "select 1"
    rm_cmds = [shlex.split(c) for c in ssh.commands if c.startswith("rm ")]
    assert len(rm_cmds) == 1
    assert rm_cmds[0][:2] == ["rm", "-rf"] and len(rm_cmds[0]) == 3
    assert re.fullmatch(
        rf"{re.escape(str(stage_root))}/dango-restore-staging-[0-9a-f]{{32}}", rm_cmds[0][2]
    )


def _build_archive(tmp_path: Path, layout: str) -> Path:
    """Build a real archive: 'dir' (backup-<ts>/...), 'dot' (./...) or 'flat' (no common dir)."""
    src = tmp_path / "src"
    (src / "data").mkdir(parents=True)
    (src / "data" / "warehouse.duckdb").write_bytes(b"db")
    archive = tmp_path / "x.tar.gz"
    if layout == "dir":
        top = tmp_path / "build" / "backup-20260224-143000"
        top.mkdir(parents=True)
        (top / "data").mkdir()
        (top / "data" / "warehouse.duckdb").write_bytes(b"db")
        cmd = ["tar", "-czf", str(archive), "-C", str(top.parent), top.name]
    elif layout == "dot":
        cmd = ["tar", "-czf", str(archive), "-C", str(src), "."]
    else:
        cmd = ["tar", "-czf", str(archive), "-C", str(src), "data"]
    subprocess.run(cmd, check=True)
    return archive


@pytest.mark.parametrize(("layout", "ok"), [("dir", True), ("dot", True), ("flat", False)])
def test_restore_layout_guard(
    layout: str, ok: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real tar: the layout guard accepts dir and ./ archives and rejects flat ones.

    A flat multi-entry archive must raise before any copy, with services
    restarted and the generated staging dir removed.
    """
    archive = _build_archive(tmp_path, layout)
    project = tmp_path / "project"
    stage_root = tmp_path / "stage"
    stage_root.mkdir()
    monkeypatch.setattr(backup, "PROJECT_DIR", str(project))
    monkeypatch.setattr(backup, "RESTORE_STAGING_ROOT", str(stage_root))
    ssh = ExecutingSSH()
    if ok:
        _patched_restore_with(ssh, str(archive))
        assert (project / "data" / "warehouse.duckdb").read_bytes() == b"db"
    else:
        with pytest.raises(CloudProvisioningError, match="unexpected layout"):
            _patched_restore_with(ssh, str(archive))
        assert not project.exists()
        assert not any(c.startswith("chown ") for c in ssh.commands)
    assert len([c for c in ssh.commands if c.startswith("rm -rf ")]) == 1


def test_failed_extraction_still_removes_only_generated_staging() -> None:
    """A failing tar step still issues exactly one rm -rf of the generated staging dir.

    Services are restarted by the outer finally; no other command removes anything.
    """

    class FailingTar(FakeSSH):
        def exec_command(
            self, command: str, timeout: int | None = None, check: bool = False
        ) -> Any:
            self.commands.append(command)
            ok = "tar -xzf" not in command
            return CommandResult(
                stdout="", stderr="" if ok else "disk full", exit_code=0 if ok else 1
            )

    ssh = FailingTar()
    with (
        patch.object(backup, "create_backup"),
        patch.object(backup, "stop_services"),
        patch.object(backup, "start_services") as start,
        pytest.raises(CloudProvisioningError, match="extract_archive"),
    ):
        backup.restore_from_archive(_as_ssh(ssh), "/srv/dango/backups/deploy/b.tar.gz")

    start.assert_called_once()
    removals = [shlex.split(c) for c in ssh.commands if "rm " in c]
    assert len(removals) == 1
    assert removals[0][:2] == ["rm", "-rf"]
    assert re.fullmatch(r"/tmp/dango-restore-staging-[0-9a-f]{32}", removals[0][2])
