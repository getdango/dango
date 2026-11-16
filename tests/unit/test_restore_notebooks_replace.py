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
from tests.factories.backup_shell import STAT_SHIM, ShellSSH, run_spaces_restore

pytestmark = pytest.mark.unit


def _shim_path(tmp_path: Path, **scripts: str) -> dict[str, str]:
    """An environment whose PATH starts with a directory of shim scripts (``stat`` is GNU-style)."""
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    for name, body in {"stat": STAT_SHIM, **scripts}.items():
        (shim / name).write_text(body)
        (shim / name).chmod(0o755)
    return {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}


def _run(cmd: str, tmp_path: Path, **scripts: str) -> subprocess.CompletedProcess[str]:
    """Run a rendered step under dash; ``runuser`` is dropped (the test user owns the tree)."""
    local = cmd.replace(br.AS_SERVICE_USER + " ", "")
    return subprocess.run(
        ["dash", "-c", local],
        capture_output=True,
        text=True,
        check=False,
        env=_shim_path(tmp_path, **scripts),
    )


def _kept(tmp_path: Path) -> Path:
    """A fresh directory standing in for the private ``dango-restore-kept`` directory."""
    kept = tmp_path / "kept"
    kept.mkdir(exist_ok=True)
    return kept


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
    cmd = br.restore_backup_dir_cmd("/s", "/p", "notebooks", "/k")
    assert subprocess.run(["dash", "-n"], input=cmd, capture_output=True, text=True).returncode == 0
    for tool in ("mkdir", "cp -RP"):
        assert cmd.count(tool) == cmd.count(f"{br.AS_SERVICE_USER} {tool}") == 1, tool
    # every mutation runs as the service user; the old directory is renamed, never emptied in place
    assert "rm -rf /p/notebooks/" not in cmd and "rm -rf /p/notebooks &&" not in cmd
    assert cmd.count(" mv ") == cmd.count(f"{br.AS_SERVICE_USER} mv ") == 3
    assert f"{br.AS_SERVICE_USER} mv /p/notebooks /k/notebooks" in cmd
    assert " -T" not in cmd


def test_planted_links_are_not_followed(tmp_path: Path) -> None:
    project, nb, stage = _tree(tmp_path)
    result = _run(
        br.restore_backup_dir_cmd(
            str(stage.parent), str(project), "notebooks", str(_kept(tmp_path))
        ),
        tmp_path,
    )
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
    result = _run(
        br.restore_backup_dir_cmd(
            str(stage.parent), str(project), "notebooks", str(_kept(tmp_path))
        ),
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in other.iterdir()) == ["keep.py"]
    assert (other / "keep.py").read_text() == "KEEP\n"
    assert nb.is_dir() and not nb.is_symlink()
    assert (nb / "a.py").read_text() == "FROM-ARCHIVE\n"


def test_missing_or_linked_source_changes_nothing(tmp_path: Path) -> None:
    project, nb, stage = _tree(tmp_path)
    before = sorted(p.name for p in nb.iterdir())
    assert (
        _run(
            br.restore_backup_dir_cmd(
                str(tmp_path / "nope"), str(project), "notebooks", str(_kept(tmp_path))
            ),
            tmp_path,
        ).returncode
        == 0
    )
    link = tmp_path / "stage-link"
    link.symlink_to(stage)
    assert (
        _run(
            br.restore_dir_cmd(
                str(link), str(nb), replace=True, kept=str(_kept(tmp_path) / "notebooks")
            ),
            tmp_path,
        ).returncode
        == 0
    )
    assert sorted(p.name for p in nb.iterdir()) == before


def test_failed_copy_fails_the_step(tmp_path: Path) -> None:
    project, nb, stage = _tree(tmp_path)
    env = _shim_path(tmp_path, cp="#!/bin/sh\necho 'cp: No space left on device' >&2\nexit 1\n")
    cmd = br.restore_backup_dir_cmd(
        str(stage.parent), str(project), "notebooks", str(_kept(tmp_path))
    ).replace(br.AS_SERVICE_USER + " ", "")
    result = subprocess.run(["dash", "-c", cmd], capture_output=True, text=True, env=env)
    assert result.returncode != 0


def test_replace_without_a_kept_path_is_refused() -> None:
    with pytest.raises(ValueError):
        br.restore_dir_cmd("/s/notebooks", "/p/notebooks", replace=True)
    with pytest.raises(ValueError):
        br.restore_backup_dir_cmd("/s", "/p", "notebooks")


def test_failed_copy_puts_the_old_directory_back(tmp_path: Path) -> None:
    project, nb, stage = _tree(tmp_path)
    env = _shim_path(tmp_path, cp="#!/bin/sh\necho 'cp: No space left on device' >&2\nexit 1\n")
    cmd = br.restore_backup_dir_cmd(
        str(stage.parent), str(project), "notebooks", str(_kept(tmp_path))
    ).replace(br.AS_SERVICE_USER + " ", "")
    result = subprocess.run(["dash", "-c", cmd], capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert nb.is_dir() and not nb.is_symlink() and (nb / "stale.py").read_text() == "stale\n"
    assert (nb / "a.py").is_symlink()
    assert [p.name for p in _kept(tmp_path).iterdir()] == ["notebooks.failed"]


@pytest.mark.parametrize("mode", [0o000, 0o755, 0o1777], ids=["mode-000", "mode-0755", "sticky"])
def test_runner_planted_directory_cannot_block_the_replace(tmp_path: Path, mode: int) -> None:
    """A subdirectory the service user cannot empty must not fail or half-do the restore."""
    project, nb, stage = _tree(tmp_path)
    blocker = nb / "blk"
    blocker.mkdir()
    (blocker / "inner.py").write_text("planted\n")
    blocker.chmod(mode)
    kept = _kept(tmp_path)
    try:
        result = _run(
            br.restore_backup_dir_cmd(str(stage.parent), str(project), "notebooks", str(kept)),
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        _assert_replaced(project, nb)
        assert (kept / "notebooks" / "stale.py").exists()  # moved aside whole, not half-deleted
        assert (kept / "notebooks" / "blk").is_dir()
    finally:
        (kept / "notebooks" / "blk").chmod(0o755)


def test_symlinked_notebooks_is_renamed_as_a_link(tmp_path: Path) -> None:
    project, nb, stage = _tree(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.py").write_text("KEEP\n")
    for child in list(nb.iterdir()):
        child.unlink()
    nb.rmdir()
    nb.symlink_to(other)
    kept = _kept(tmp_path)
    result = _run(
        br.restore_backup_dir_cmd(str(stage.parent), str(project), "notebooks", str(kept)), tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert (kept / "notebooks").is_symlink() and (other / "keep.py").read_text() == "KEEP\n"
    assert nb.is_dir() and not nb.is_symlink()


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
        _run(
            br.restore_backup_dir_cmd(str(tmp_path / "s"), str(project), "scripts"), tmp_path
        ).returncode
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
    assert not [p for p in root.iterdir() if "dango-restore-" in p.name], "leftover private dirs"


def test_scheduled_restore_replaces_notebooks(tmp_path: Path) -> None:
    _archive, project, nb = _archive_and_project(tmp_path)
    run_spaces_restore(tmp_path, project, "backup-1")
    _assert_replaced(project, nb)


_PARTIAL_CP = (
    "#!/bin/sh\n/bin/cp \"$@\"\necho 'cp: injected failure after a partial copy' >&2\nexit 1\n"
)


def _poison_archive(stage: Path) -> None:
    """Runner-planted modes that survive a backup: a 0555 and a mode-000 directory with files."""
    for name, mode in (("r1", 0o555), ("zz", 0o000)):
        (stage / name).mkdir()
        (stage / name / "q.py").write_text(f"{name}\n")
        (stage / name).chmod(mode)


def _unpoison(*roots: Path) -> None:
    for root in roots:
        for path in [root, *root.rglob("*")] if root.exists() else []:
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)


def test_poisoned_archive_restores_completely(tmp_path: Path) -> None:
    project, nb, stage = _tree(tmp_path)
    _poison_archive(stage)
    kept = _kept(tmp_path)
    try:
        result = _run(
            br.restore_backup_dir_cmd(str(stage.parent), str(project), "notebooks", str(kept)),
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert (nb / "r1" / "q.py").read_text() == "r1\n"
        assert (nb / "zz" / "q.py").read_text() == "zz\n"
        assert (nb / "a.py").read_text() == "FROM-ARCHIVE\n"
    finally:
        _unpoison(stage, nb, kept)


def test_staged_notebooks_are_made_writable_without_following_links() -> None:
    cmd = br.restore_backup_dir_cmd("/s", "/p", "notebooks", "/k")
    chmod = f"{br.AS_SERVICE_USER} chmod -R u+rwX /s/notebooks"
    assert chmod in cmd
    assert cmd.index(chmod) < cmd.index("cp -RP")
    assert (
        " -L" not in chmod and " -H" not in chmod
    )  # -R alone never follows links below the operand
    assert "chmod" not in br.restore_dir_cmd("/s/scripts", "/p/scripts")


def test_failed_copy_over_poisoned_tree_puts_old_notebooks_back(tmp_path: Path) -> None:
    project, nb, stage = _tree(tmp_path)
    _poison_archive(stage)
    kept = _kept(tmp_path)
    try:
        result = _run(
            br.restore_backup_dir_cmd(str(stage.parent), str(project), "notebooks", str(kept)),
            tmp_path,
            cp=_PARTIAL_CP,
        )
        assert result.returncode != 0
        # the half-copied 0555 directory could not be emptied: it is renamed aside instead
        assert (nb / "stale.py").read_text() == "stale\n" and (nb / "a.py").is_symlink()
        assert not (nb / "r1").exists()
        assert (kept / "notebooks.failed" / "r1" / "q.py").exists()
        assert not (kept / "notebooks").exists()
    finally:
        _unpoison(stage, nb, kept)


def test_undo_never_empties_the_failed_directory_in_place() -> None:
    cmd = br.restore_backup_dir_cmd("/s", "/p", "notebooks", "/k")
    assert "rm " not in cmd
    assert f"{br.AS_SERVICE_USER} mv /p/notebooks /k/notebooks.failed" in cmd


def test_different_filesystem_is_refused_before_anything_changes(tmp_path: Path) -> None:
    project, nb, stage = _tree(tmp_path)
    kept = _kept(tmp_path)
    before = sorted(p.name for p in nb.iterdir())
    cmd = br.restore_backup_dir_cmd(str(stage.parent), str(project), "notebooks", str(kept))
    local = cmd.replace(br.AS_SERVICE_USER + " ", "")
    env = {**_shim_path(tmp_path), "STAT_OTHER_DEVICE": str(nb)}
    result = subprocess.run(["dash", "-c", local], capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert "different filesystems" in result.stderr
    assert sorted(p.name for p in nb.iterdir()) == before
    assert list(kept.iterdir()) == []


def test_filesystem_check_is_rendered_as_service_user_before_the_move() -> None:
    cmd = br.restore_backup_dir_cmd("/s", "/p", "notebooks", "/k")
    assert f"{br.AS_SERVICE_USER} stat -c %d /p/notebooks" in cmd
    assert f"{br.AS_SERVICE_USER} stat -c %d /k" in cmd
    assert cmd.index("stat -c %d") < cmd.index(f"{br.AS_SERVICE_USER} mv /p/notebooks /k/notebooks")


def test_same_filesystem_and_missing_notebooks_pass_the_check(tmp_path: Path) -> None:
    project, nb, stage = _tree(tmp_path)
    for child in list(nb.iterdir()):
        child.unlink()
    nb.rmdir()  # nothing to move aside: no filesystem check is needed
    result = _run(
        br.restore_backup_dir_cmd(
            str(stage.parent), str(project), "notebooks", str(_kept(tmp_path))
        ),
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert (nb / "a.py").read_text() == "FROM-ARCHIVE\n"
