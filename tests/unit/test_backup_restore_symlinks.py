"""tests/unit/test_backup_restore_symlinks.py

Server restores replace symlinks in the destination instead of following them.
"""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.factories.backup_shell import ShellSSH

_B = "dango.platform.cloud.backup"
_SB = "dango.platform.cloud.scheduled_backup"
_REAL_RUN = subprocess.run
_TOP = "backup-20260101-000000"


def _content(root: Path) -> None:
    """Archive content: a file, a directory file, and a file in a second directory."""
    (root / ".dango").mkdir(parents=True)
    (root / ".dango" / "project.yml").write_text("from-archive\n")
    (root / "dbt" / "models").mkdir(parents=True)
    (root / "dbt" / "models" / "m.sql").write_text("select 'archive'\n")
    (root / "dbt" / "macros").mkdir(parents=True)
    (root / "dbt" / "macros" / "x.sql").write_text("-- archive macro\n")
    (root / "manifest.json").write_text("{}")


def _hostile_project(tmp_path: Path) -> tuple[Path, Path]:
    """A live project whose file, directory and in-tree file are symlinks to outside targets."""
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "target.txt").write_text("outside\n")
    (outside / "dir" / "m.sql").write_text("outside model\n")
    (outside / "macro.sql").write_text("outside macro\n")

    project = tmp_path / "project"
    (project / ".dango").mkdir(parents=True)
    (project / ".dango" / "project.yml").symlink_to(outside / "target.txt")
    (project / "dbt" / "macros").mkdir(parents=True)
    (project / "dbt" / "models").symlink_to(outside / "dir", target_is_directory=True)
    (project / "dbt" / "macros" / "x.sql").symlink_to(outside / "macro.sql")
    return project, outside


def _assert_not_followed(project: Path, outside: Path) -> None:
    assert (outside / "target.txt").read_text() == "outside\n"
    assert (outside / "dir" / "m.sql").read_text() == "outside model\n"
    assert (outside / "macro.sql").read_text() == "outside macro\n"
    for rel, text in [
        (".dango/project.yml", "from-archive\n"),
        ("dbt/models/m.sql", "select 'archive'\n"),
        ("dbt/macros/x.sql", "-- archive macro\n"),
    ]:
        path = project / rel
        assert not path.is_symlink() and path.is_file(), rel
        assert path.read_text() == text
    assert not (project / "dbt" / "models").is_symlink()


@pytest.mark.unit
def test_server_restore_replaces_destination_symlinks(tmp_path: Path) -> None:
    from dango.platform.cloud.backup import restore_from_archive

    project, outside = _hostile_project(tmp_path)
    content = tmp_path / "content"
    _content(content)
    archive = tmp_path / f"{_TOP}.tar.gz"
    with tarfile.open(archive, mode="w:gz") as tf:
        tf.add(str(content), arcname=_TOP)
    (tmp_path / "staging-root").mkdir()

    with (
        patch(f"{_B}.PROJECT_DIR", str(project)),
        patch(f"{_B}.RESTORE_STAGING_ROOT", str(tmp_path / "staging-root")),
        patch(f"{_B}.create_backup", return_value=MagicMock(archive_path="safety.tar.gz")),
        patch(f"{_B}.stop_services"),
        patch(f"{_B}.start_services"),
        patch(f"{_B}.verify_health", return_value=True),
        patch(f"{_B}._get_metabase_volume_path", return_value=None),
    ):
        restore_from_archive(ShellSSH(tmp_path / "shim"), str(archive))

    _assert_not_followed(project, outside)


@pytest.mark.unit
def test_spaces_restore_replaces_destination_symlinks(tmp_path: Path) -> None:
    from dango.platform.cloud import scheduled_backup as sb

    project, outside = _hostile_project(tmp_path)
    _content(tmp_path / _TOP)  # already "extracted" into the staging dir
    real_path = Path

    def fake_path(value: Any = ".") -> Path:
        text = str(value)
        if text.startswith("/tmp/"):
            return tmp_path / text[len("/tmp/") :]
        return real_path(value)

    def run_local(command: str, **_kw: Any) -> str:
        if "tar -xzf" in command or command.startswith("chown"):
            return ""
        proc = _REAL_RUN(command, shell=True, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    with (
        patch(f"{_SB}.PROJECT_DIR", project),
        patch(f"{_SB}.BACKUP_DIR", tmp_path / "backups"),
        patch(f"{_SB}.download_from_spaces"),
        patch(f"{_SB}.Path", fake_path),
        patch(f"{_SB}._stop_services"),
        patch(f"{_SB}._start_services"),
        patch(f"{_SB}._run_local", side_effect=run_local),
        patch(f"{_SB}._get_metabase_volume_path", return_value=None),
        patch(f"{_SB}.subprocess.run"),
    ):
        sb.restore_from_spaces({}, f"backups/{_TOP}.tar.gz")

    _assert_not_followed(project, outside)


@pytest.mark.unit
def test_spaces_restore_does_not_write_metabase_yaml_through_symlink(tmp_path: Path) -> None:
    from dango.platform.cloud import scheduled_backup as sb

    outside = tmp_path / "outside.yml"
    outside.write_text("outside\n")
    project = tmp_path / "project"
    (project / ".dango").mkdir(parents=True)
    (project / ".dango" / "metabase.yml").symlink_to(outside)
    staging = tmp_path / _TOP
    (staging / ".dango").mkdir(parents=True)
    (staging / ".dango" / "metabase.yml").write_text("url: http://x\n")
    real_path = Path

    def fake_path(value: Any = ".") -> Path:
        text = str(value)
        if text.startswith("/tmp/"):
            return tmp_path / text[len("/tmp/") :]
        return real_path(value)

    with (
        patch(f"{_SB}.PROJECT_DIR", project),
        patch(f"{_SB}.BACKUP_DIR", tmp_path / "backups"),
        patch(f"{_SB}.download_from_spaces"),
        patch(f"{_SB}.Path", fake_path),
        patch(f"{_SB}._stop_services"),
        patch(f"{_SB}._start_services"),
        patch(f"{_SB}._run_local", return_value=""),
        patch(f"{_SB}._get_metabase_volume_path", return_value=None),
        patch(f"{_SB}.subprocess.run"),
    ):
        sb.restore_from_spaces({}, f"backups/{_TOP}.tar.gz")

    assert outside.read_text() == "outside\n"
    dest = project / ".dango" / "metabase.yml"
    assert not dest.is_symlink() and "http://x" in dest.read_text()


@pytest.mark.unit
def test_server_restore_unlinks_metabase_yaml_symlink_before_writing(tmp_path: Path) -> None:
    from dango.platform.cloud.backup import restore_from_archive

    outside = tmp_path / "outside.yml"
    outside.write_text("outside\n")
    project = tmp_path / "project"
    (project / ".dango").mkdir(parents=True)
    (project / ".dango" / "metabase.yml").symlink_to(outside)
    content = tmp_path / "content"
    _content(content)
    (content / ".dango" / "metabase.yml").write_text("url: http://x\n")
    archive = tmp_path / f"{_TOP}.tar.gz"
    with tarfile.open(archive, mode="w:gz") as tf:
        tf.add(str(content), arcname=_TOP)
    (tmp_path / "staging-root").mkdir()
    ssh = ShellSSH(tmp_path / "shim")

    with (
        patch(f"{_B}.PROJECT_DIR", str(project)),
        patch(f"{_B}.RESTORE_STAGING_ROOT", str(tmp_path / "staging-root")),
        patch(f"{_B}.create_backup", return_value=MagicMock(archive_path="safety.tar.gz")),
        patch(f"{_B}.stop_services"),
        patch(f"{_B}.start_services"),
        patch(f"{_B}.verify_health", return_value=True),
        patch(f"{_B}._get_metabase_volume_path", return_value=None),
    ):
        restore_from_archive(ssh, str(archive))

    # The sanitizer (server venv, stubbed here) must find no symlink at its destination.
    sanitize_at = next(i for i, c in enumerate(ssh.commands) if c.startswith("/srv/dango/venv"))
    assert not (project / ".dango" / "metabase.yml").is_symlink()
    assert any("metabase.yml" in c and "-L" in c for c in ssh.commands[:sanitize_at]), (
        "symlink not removed before the metabase.yml write"
    )
    assert outside.read_text() == "outside\n"
