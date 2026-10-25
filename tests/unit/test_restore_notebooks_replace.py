"""tests/unit/test_restore_notebooks_replace.py

Restoring ``notebooks/`` replaces the directory (as the service user, never following links);
every other directory keeps its merge behaviour.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.cloud import backup
from dango.platform.cloud import backup_restore as br
from tests.factories.backup_shell import ShellSSH, run_spaces_restore

pytestmark = pytest.mark.unit


def _run(cmd: str) -> subprocess.CompletedProcess[str]:
    """Run a rendered step under dash; ``runuser`` is dropped (the test user owns the tree)."""
    local = cmd.replace(br.AS_SERVICE_USER + " ", "")
    return subprocess.run(["dash", "-c", local], capture_output=True, text=True, check=False)


def _tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Project with hostile notebooks content, a scripts/x.py, and a staged archive copy."""
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts" / "x.py").write_text("ORIGINAL\n")
    nb = project / "notebooks"
    nb.mkdir()
    (nb / "a.py").symlink_to("../scripts/x.py")
    (nb / "dangling.py").symlink_to("../scripts/new.py")
    (nb / "linkdir").symlink_to("../scripts")
    os.mkfifo(nb / "pipe")
    (nb / "stale.py").write_text("stale\n")
    stage = tmp_path / "stage" / "notebooks"
    stage.mkdir(parents=True)
    (stage / "a.py").write_text("FROM-ARCHIVE\n")
    (stage / "dangling.py").write_text("D\n")
    (stage / "linkdir").mkdir()
    (stage / "linkdir" / "x.py").write_text("NESTED\n")
    return project, nb, stage


def _assert_replaced(project: Path, nb: Path) -> None:
    assert (project / "scripts" / "x.py").read_text() == "ORIGINAL\n"
    assert not (project / "scripts" / "new.py").exists()
    assert not (project / "scripts" / "linkdir").exists()
    assert nb.is_dir() and not nb.is_symlink()
    assert (nb / "a.py").read_text() == "FROM-ARCHIVE\n" and not (nb / "a.py").is_symlink()
    assert (nb / "dangling.py").read_text() == "D\n" and not (nb / "dangling.py").is_symlink()
    assert (nb / "linkdir" / "x.py").read_text() == "NESTED\n" and not (nb / "linkdir").is_symlink()
    assert not (nb / "pipe").exists() and not (nb / "stale.py").exists()
    assert sorted(p.name for p in nb.iterdir()) == ["a.py", "dangling.py", "linkdir"]
    assert stat.S_IMODE(nb.stat().st_mode) == 0o755


def test_replace_command_is_valid_dash_and_runs_as_service_user() -> None:
    cmd = br.restore_backup_dir_cmd("/s", "/p", "notebooks")
    assert subprocess.run(["dash", "-n"], input=cmd, capture_output=True, text=True).returncode == 0
    for tool in ("rm -rf", "mkdir", "cp -RP"):
        assert cmd.count(tool) == cmd.count(f"{br.AS_SERVICE_USER} {tool}") == 1, tool
    assert "rm -rf /p/notebooks &&" in cmd and "rm -rf /p/notebooks/" not in cmd


def test_planted_links_are_not_followed(tmp_path: Path) -> None:
    project, nb, stage = _tree(tmp_path)
    result = _run(br.restore_backup_dir_cmd(str(stage), str(nb), "notebooks"))
    assert result.returncode == 0, result.stderr
    _assert_replaced(project, nb)


def test_notebooks_itself_a_symlink_is_removed_not_followed(tmp_path: Path) -> None:
    project, nb, stage = _tree(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.py").write_text("KEEP\n")
    for child in list(nb.iterdir()):
        child.unlink()
    nb.rmdir()
    nb.symlink_to(other)
    result = _run(br.restore_backup_dir_cmd(str(stage), str(nb), "notebooks"))
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in other.iterdir()) == ["keep.py"]
    assert (other / "keep.py").read_text() == "KEEP\n"
    assert nb.is_dir() and not nb.is_symlink()
    assert (nb / "a.py").read_text() == "FROM-ARCHIVE\n"


def test_missing_or_linked_source_changes_nothing(tmp_path: Path) -> None:
    project, nb, stage = _tree(tmp_path)
    before = sorted(p.name for p in nb.iterdir())
    assert (
        _run(br.restore_backup_dir_cmd(str(tmp_path / "nope"), str(nb), "notebooks")).returncode
        == 0
    )
    link = tmp_path / "stage-link"
    link.symlink_to(stage)
    assert _run(br.restore_backup_dir_cmd(str(link), str(nb), "notebooks")).returncode == 0
    assert sorted(p.name for p in nb.iterdir()) == before


def test_failed_copy_fails_the_step(tmp_path: Path) -> None:
    _project, nb, stage = _tree(tmp_path)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "cp").write_text("#!/bin/sh\necho 'cp: No space left on device' >&2\nexit 1\n")
    (shim / "cp").chmod(0o755)
    env = {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}
    cmd = br.restore_backup_dir_cmd(str(stage), str(nb), "notebooks").replace(
        br.AS_SERVICE_USER + " ", ""
    )
    result = subprocess.run(["dash", "-c", cmd], capture_output=True, text=True, env=env)
    assert result.returncode != 0


def test_other_directories_keep_merge_behaviour(tmp_path: Path) -> None:
    golden = br.restore_dir_cmd("/stage/scripts", "/p/scripts")
    for rel in ("data", "scripts", "dbt", "custom_sources"):
        assert br.restore_backup_dir_cmd("/stage", "/p", rel) == br.restore_dir_cmd(
            f"/stage/{rel}", f"/p/{rel}"
        )
    assert "rm -rf" not in golden
    assert "cp -RP /stage/scripts/. /p/scripts/" in golden
    project = tmp_path / "p"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts" / "keep.py").write_text("K\n")
    stage = tmp_path / "s" / "scripts"
    stage.mkdir(parents=True)
    (stage / "new.py").write_text("N\n")
    assert (
        _run(br.restore_backup_dir_cmd(str(tmp_path / "s"), str(project), "scripts")).returncode
        == 0
    )
    assert sorted(p.name for p in (project / "scripts").iterdir()) == ["keep.py", "new.py"]


def test_only_notebooks_is_replaced() -> None:
    assert br.REPLACE_ON_RESTORE == ("notebooks",)


def _archive_and_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project, nb, stage = _tree(tmp_path)
    top = tmp_path / "backup-1"
    (top / "notebooks").mkdir(parents=True)
    for item in stage.iterdir():
        if item.is_dir():
            (top / "notebooks" / item.name).mkdir()
            (top / "notebooks" / item.name / "x.py").write_text("NESTED\n")
        else:
            (top / "notebooks" / item.name).write_text(item.read_text())
    (top / "manifest.json").write_text("{}")
    archive = tmp_path / "backup-1.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(str(top), arcname="backup-1")
    return archive, project, nb


def test_server_restore_replaces_notebooks(tmp_path: Path) -> None:
    archive, project, nb = _archive_and_project(tmp_path)
    root = tmp_path / "root"
    ssh = ShellSSH(tmp_path / "shim")
    with (
        patch.object(backup, "PROJECT_DIR", str(project)),
        patch.object(backup, "RESTORE_STAGING_ROOT", str(root)),
        patch.object(backup, "create_backup", return_value=MagicMock(archive_path="s")),
        patch.object(backup, "stop_services"),
        patch.object(backup, "start_services"),
        patch.object(backup, "verify_health", return_value=True),
        patch.object(backup, "_get_metabase_volume_path", return_value=None),
    ):
        backup.restore_from_archive(ssh, str(archive))
    _assert_replaced(project, nb)


def test_scheduled_restore_replaces_notebooks(tmp_path: Path) -> None:
    _archive, project, nb = _archive_and_project(tmp_path)
    with patch.object(br, "RESTORE_PRIVATE_ROOT", str(tmp_path / "root"), create=True):
        run_spaces_restore(tmp_path, project, "backup-1")
    _assert_replaced(project, nb)
