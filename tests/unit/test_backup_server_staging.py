"""tests/unit/test_backup_server_staging.py

The SSH backup stages in a fresh private directory and writes a private archive without following links.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from dango.platform.cloud.ssh import CommandResult
from tests.factories.backup_create import run_server_archive, unique_timestamp
from tests.factories.backup_shell import ShellSSH, make_duckdb


class _ObservingSSH(ShellSSH):
    """ShellSSH that records the staging tree's mode when the archive is written."""

    def __init__(self, shim_dir: Path, staging_root: Path) -> None:
        super().__init__(shim_dir)
        self.staging_root = staging_root
        self.modes: dict[str, int] = {}

    def exec_command(self, command: str, timeout: int = 60, **kwargs: object) -> CommandResult:
        if "tar -czf" in command or "tar -cz" in command:
            for entry in self.staging_root.iterdir():
                self.modes[entry.name] = stat.S_IMODE(entry.lstat().st_mode)
        return super().exec_command(command, timeout, **kwargs)


def _project(root: Path) -> Path:
    make_duckdb(root / "data" / "warehouse.duckdb", "live")
    (root / ".dango").mkdir()
    (root / ".dango" / "project.yml").write_text("project: {}\n")
    (root / "data" / "warehouse.duckdb").chmod(0o644)
    return root


@pytest.mark.unit
def test_predictable_staging_dir_or_symlink_is_never_used(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    staging_root = tmp_path / "staging-root"
    staging_root.mkdir()
    ts = unique_timestamp()
    planted_dir = staging_root / f"backup-{ts}"
    planted_dir.mkdir()
    (planted_dir / "planted.txt").write_text("attacker")
    elsewhere = tmp_path / "attacker-owned"
    elsewhere.mkdir()

    ssh = _ObservingSSH(tmp_path / "shim", staging_root)
    _archive, members, _warnings, ssh = run_server_archive(
        tmp_path, project, staging_root=staging_root, timestamp=ts, ssh=ssh
    )

    assert "planted.txt" not in members
    assert sorted(p.name for p in planted_dir.iterdir()) == ["planted.txt"]
    assert not list(elsewhere.iterdir())
    used = [name for name in ssh.modes if name != planted_dir.name]
    assert used, f"backup did not stage under the staging root: {ssh.commands[:3]}"
    assert all(ssh.modes[name] == 0o700 for name in used), ssh.modes
    # Cleaned up afterwards; the planted directory is left alone.
    assert sorted(p.name for p in staging_root.iterdir()) == [planted_dir.name]


@pytest.mark.unit
def test_planted_symlink_at_staging_name_is_not_followed(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    staging_root = tmp_path / "staging-root"
    staging_root.mkdir()
    ts = unique_timestamp()
    target = tmp_path / "attacker-owned"
    target.mkdir()
    (staging_root / f"backup-{ts}").symlink_to(target, target_is_directory=True)

    run_server_archive(tmp_path, project, staging_root=staging_root, timestamp=ts)

    assert not list(target.iterdir()), "backup wrote through a planted staging symlink"


@pytest.mark.unit
def test_server_archive_and_manifest_are_private_members_keep_modes(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    previous = os.umask(0o022)
    try:
        archive, members, _warnings, _ssh = run_server_archive(tmp_path, project)
    finally:
        os.umask(previous)
    manifest = archive.with_name(archive.name.replace(".tar.gz", ".json"))
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert stat.S_IMODE(members["data/warehouse.duckdb"].mode) == 0o644


@pytest.mark.unit
def test_server_archive_is_not_written_through_a_planted_link(tmp_path: Path) -> None:
    from dango.exceptions import CloudProvisioningError

    project = _project(tmp_path / "project")
    ts = unique_timestamp()
    backups = tmp_path / "backups" / "deploy"
    backups.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    (backups / f"backup-{ts}.tar.gz").symlink_to(victim)

    with pytest.raises(CloudProvisioningError):
        run_server_archive(tmp_path, project, timestamp=ts)

    assert victim.read_text() == "keep me"
