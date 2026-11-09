"""tests/unit/test_g4f1_r2_fixes.py

G4F1 review round 2: the deploy lock cannot be created through, or read from, anything the
service user plants; ``_get_last_backup`` runs as dango; a linked ``data/`` copies only
entries the service user owns.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.ssh import CommandResult

_SB = "dango.platform.cloud.scheduled_backup"


class LockServer:
    """Runs deploy-lock commands in a sandbox ``/srv/dango``; a ``mkdir`` shim can plant a FIFO link."""

    def __init__(self, tmp_path: Path, *, plant_on_mkdir: bool = False) -> None:
        self.root = tmp_path / "srv" / "dango"
        self.root.mkdir(parents=True)
        self.lock = self.root / "deploy.lock"
        self.fifo = tmp_path / "fifo"
        os.mkfifo(self.fifo)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        if plant_on_mkdir:
            shim = bin_dir / "mkdir"
            shim.write_text(
                f'#!/bin/sh\n[ -e "{self.lock}" ] || [ -L "{self.lock}" ] || ln -s {self.fifo} "{self.lock}"\n'
                'exec /bin/mkdir "$@"\n'
            )
            shim.chmod(0o755)
        self.path = f"{bin_dir}:/usr/bin:/bin"
        self.timed_out = False

    def exec_command(self, command: str, timeout: int | None = None, **_: object) -> CommandResult:
        try:
            proc = subprocess.run(
                ["sh", "-c", command.replace("/srv/dango", str(self.root))],
                capture_output=True,
                text=True,
                env={"PATH": self.path},
                timeout=4,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.timed_out = True
            return CommandResult(stdout="", stderr="timeout", exit_code=124)
        return CommandResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)


@pytest.mark.unit
class TestDeployLockCannotBePlantedThrough:
    def test_a_link_to_a_fifo_planted_during_the_create_is_never_opened(
        self, tmp_path: Path
    ) -> None:
        from dango.platform.cloud.deployer import _acquire_lock

        server = LockServer(tmp_path, plant_on_mkdir=True)
        with pytest.raises(CloudProvisioningError):
            _acquire_lock(server)
        assert not server.timed_out, "root opened the planted FIFO and blocked"

    @pytest.mark.parametrize("kind", ["fifo-link", "file-link", "file", "dir"])
    def test_an_entry_created_by_dango_counts_as_held_and_is_never_written(
        self, tmp_path: Path, kind: str
    ) -> None:
        from dango.platform.cloud.deployer import _acquire_lock

        server = LockServer(tmp_path)
        victim = tmp_path / "victim"
        victim.write_text("untouched")
        if kind == "fifo-link":
            server.lock.symlink_to(server.fifo)
        elif kind == "file-link":
            server.lock.symlink_to(victim)
        elif kind == "file":
            server.lock.write_text("not json")
        else:
            server.lock.mkdir()
        with pytest.raises(CloudProvisioningError, match="[Dd]eploy lock"):
            _acquire_lock(server)
        assert not server.timed_out
        assert victim.read_text() == "untouched"

    def test_acquire_release_and_force_round_trip(self, tmp_path: Path) -> None:
        from dango.platform.cloud.deployer import (
            _acquire_lock,
            _check_existing_lock,
            _release_lock,
        )

        server = LockServer(tmp_path)
        out: list[str] = []
        lock = _acquire_lock(server, lock_json_out=out)
        held = _check_existing_lock(server)
        assert held is not None and held.deployer == lock.deployer
        with pytest.raises(CloudProvisioningError, match="Deploy lock held"):
            _acquire_lock(server)
        assert _acquire_lock(server, force=True) is not None
        _release_lock(server)
        assert _check_existing_lock(server) is None
        assert not server.lock.exists() and not server.lock.is_symlink()

    def test_release_if_ours_removes_only_our_lock(self, tmp_path: Path) -> None:
        from dango.platform.cloud.deployer import _acquire_lock, _release_lock_if_ours

        server = LockServer(tmp_path)
        out: list[str] = []
        _acquire_lock(server, lock_json_out=out)
        _release_lock_if_ours(server, '{"someone": "else"}')
        assert server.lock.exists()
        _release_lock_if_ours(server, out[0])
        assert not server.lock.exists()


@pytest.mark.unit
def test_last_backup_is_read_as_dango() -> None:
    from dango.platform.cloud.server_status import _get_last_backup

    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(
        stdout="backup-1.tar.gz\n", stderr="", exit_code=0
    )
    assert _get_last_backup(ssh) == "backup-1.tar.gz"
    assert ssh.exec_command.call_args[0][0].startswith("runuser -u dango --")


def _create(project: Path, backups: Path) -> list[str]:
    import tarfile

    from dango.platform.cloud.scheduled_backup import _create_local_archive

    with (
        patch(f"{_SB}.PROJECT_DIR", project),
        patch(f"{_SB}.BACKUP_DIR", backups),
        patch(f"{_SB}._stop_services"),
        patch(f"{_SB}._start_services"),
        patch(f"{_SB}._checkpoint_databases", return_value=[]),
        patch(f"{_SB}._get_metabase_volume_path", return_value=None),
    ):
        archive, _manifest, warnings = _create_local_archive("test")
    with tarfile.open(archive) as tar:
        return [*tar.getnames(), *warnings]


@pytest.mark.unit
class TestLinkedDataCopiesOnlyServiceUserEntries:
    def test_a_member_owned_by_someone_else_is_left_out(self, tmp_path: Path) -> None:
        import dango.platform.cloud.backup_restore as br

        project = tmp_path / "project"
        project.mkdir()
        target = tmp_path / "srv"
        (target / "sub").mkdir(parents=True)
        (target / "mine.txt").write_text("mine")
        (target / "root-only.txt").write_text("secret")
        (target / "sub" / "root-only-too.txt").write_text("secret")
        (project / "data").symlink_to(target, target_is_directory=True)
        foreign = {(target / n).stat().st_ino for n in ("root-only.txt", "sub/root-only-too.txt")}
        real_fstat = os.fstat

        def fstat(fd: int) -> Any:
            info = real_fstat(fd)
            if info.st_ino in foreign:
                fields = list(info)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return info

        me = os.environ.get("USER") or os.getlogin()
        with patch.object(br, "BACKUP_SOURCE_OWNER", me), patch("os.fstat", fstat):
            names = _create(project, tmp_path / "backups")

        assert any(n.endswith("data/mine.txt") for n in names), names
        assert not any("root-only" in n and "data/" in n for n in names), names
