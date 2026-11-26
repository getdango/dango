"""tests/unit/test_project_files_staging_cleanup.py

``dango remote env`` writes as the ``dango`` SSH user through a private ``mktemp -d`` staging directory:
the directory is removed when the final write command raises or fails, never when its name looks wrong.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dango.exceptions import CloudProvisioningError, CloudSSHError
from dango.platform.cloud.project_files import write_project_file

pytestmark = pytest.mark.unit

_ENV = "/srv/dango/project/.env"


class FailingSSH:
    """Fake ``dango`` session: ``mktemp -d`` answers *stage*; the write command raises."""

    username = "dango"

    def __init__(self, stage: str, *, cleanup_raises: bool = False) -> None:
        self.stage = stage
        self.cleanup_raises = cleanup_raises
        self.commands: list[str] = []

    def exec_command(self, command: str, timeout: int | None = None, check: bool = False) -> Any:
        self.commands.append(command)
        if command == "mktemp -d":
            return SimpleNamespace(stdout=f"{self.stage}\n", stderr="", exit_code=0, success=True)
        if command.startswith("rm -rf") and self.cleanup_raises:
            raise CloudSSHError("connection lost during cleanup")
        if command.startswith("sh -c"):
            raise CloudSSHError("command timed out")
        return SimpleNamespace(stdout="", stderr="", exit_code=0, success=True)

    def write_remote_file(self, path: str, content: Any, mode: int = 0o644) -> None:
        return None

    def upload_file(self, local: Path, remote: str) -> None:
        return None


def _removals(ssh: FailingSSH) -> list[str]:
    return [c for c in ssh.commands if c.startswith("rm -rf")]


def test_a_write_command_that_raises_still_removes_the_staging_directory() -> None:
    ssh = FailingSSH("/tmp/tmp.AbC123")
    with pytest.raises(CloudSSHError, match="command timed out"):
        write_project_file(ssh, _ENV, "A=1\n", mode=0o600)
    assert _removals(ssh) == ["rm -rf /tmp/tmp.AbC123"]
    assert ssh.commands.index(_removals(ssh)[0]) > ssh.commands.index(
        next(c for c in ssh.commands if c.startswith("sh -c"))
    )


def test_a_failing_cleanup_never_replaces_the_original_error() -> None:
    ssh = FailingSSH("/tmp/tmp.AbC123", cleanup_raises=True)
    with pytest.raises(CloudSSHError, match="command timed out"):
        write_project_file(ssh, _ENV, "A=1\n", mode=0o600)
    assert _removals(ssh)  # it was attempted


@pytest.mark.parametrize("stage", ["/", "/tmp", "/tmp/../etc", "/tmp/a b", "/tmp/$(x)"])
def test_an_odd_mktemp_answer_is_refused_and_nothing_is_removed(stage: str) -> None:
    ssh = FailingSSH(stage)
    with pytest.raises(CloudProvisioningError, match="staging directory"):
        write_project_file(ssh, _ENV, "A=1\n", mode=0o600)
    assert _removals(ssh) == []
    assert not [c for c in ssh.commands if c.startswith("sh -c")]


def test_an_upload_error_removes_only_the_validated_staging_directory() -> None:
    class UploadFails(FailingSSH):
        def write_remote_file(self, path: str, content: Any, mode: int = 0o644) -> None:
            raise CloudSSHError("sftp dropped")

    ssh = UploadFails("/tmp/tmp.AbC123")
    with pytest.raises(CloudSSHError, match="sftp dropped"):
        write_project_file(ssh, _ENV, "A=1\n", mode=0o600)
    assert _removals(ssh) == ["rm -rf /tmp/tmp.AbC123"]


class ShellSSH:
    """Fake ``dango`` session that runs each command in a real local ``sh`` (TMPDIR = *tmp*)."""

    username = "dango"

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp

    def exec_command(self, command: str, timeout: int | None = None, check: bool = False) -> Any:
        env = {**os.environ, "TMPDIR": str(self.tmp)}
        done = subprocess.run(["sh", "-c", command], capture_output=True, text=True, env=env)
        return SimpleNamespace(
            stdout=done.stdout,
            stderr=done.stderr,
            exit_code=done.returncode,
            success=done.returncode == 0,
        )

    def write_remote_file(self, path: str, content: Any, mode: int = 0o644) -> None:
        data = content.encode() if isinstance(content, str) else content
        Path(path).write_bytes(data)
        Path(path).chmod(mode)

    def upload_file(self, local: Path, remote: str) -> None:
        Path(remote).write_bytes(local.read_bytes())


def test_a_failed_write_script_leaves_no_staging_directory(tmp_path: Path) -> None:
    """The write command returns non-zero (the destination is a directory): the status is kept
    and the staging directory is gone."""
    stage_root = tmp_path / "tmp"
    stage_root.mkdir()
    destination = tmp_path / "project" / ".env"
    destination.mkdir(parents=True)
    with pytest.raises(CloudProvisioningError, match="refusing to replace directory"):
        write_project_file(ShellSSH(stage_root), str(destination), "A=1\n", mode=0o600)
    assert list(stage_root.iterdir()) == []
    assert destination.is_dir()
