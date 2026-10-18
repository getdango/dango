"""tests/unit/test_backup_copy_final_component.py

Backup copies never follow a link at the final path component, even one swapped in after the earlier link check.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.backup_restore import (
    backup_dir_cmd,
    backup_file_cmd,
    copy_file_cmd,
    restore_dir_cmd,
)


def _sh(cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=False)


@pytest.mark.unit
def test_file_copy_commands_do_not_follow_links() -> None:
    assert "cp -P " in copy_file_cmd("/p/a", "/s")
    assert "cp -P " in backup_file_cmd("/p/a", "/s", "a")


@pytest.mark.unit
@pytest.mark.parametrize("rel", ["notebooks", "data"])
def test_dir_backup_copies_the_directory_operand_not_its_contents(rel: str) -> None:
    cmd = backup_dir_cmd(f"/p/{rel}", f"/s/{rel}", rel)
    assert "/." not in cmd.replace("2>/dev/null", "")


@pytest.mark.unit
def test_dir_backup_copies_dotfiles_and_empty_directories(tmp_path: Path) -> None:
    src = tmp_path / "p" / "notebooks"
    (src / "empty").mkdir(parents=True)
    (src / ".hidden").write_text("h")
    (src / "n.py").write_text("n")
    dest = tmp_path / "s" / "notebooks"
    result = _sh(backup_dir_cmd(str(src), str(dest), "notebooks"))
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in dest.iterdir()) == [".hidden", "empty", "n.py"]


@pytest.mark.unit
def test_linked_data_dir_is_copied_by_name_without_leftovers(tmp_path: Path) -> None:
    real = tmp_path / "block"
    real.mkdir()
    (real / "warehouse.duckdb").write_text("w")
    (real / ".hidden").write_text("h")
    link = tmp_path / "p" / "data"
    link.parent.mkdir()
    link.symlink_to(real, target_is_directory=True)
    dest = tmp_path / "s" / "data"
    # No user "dango" locally: stand in for the ownership check with the current user.
    import dango.platform.cloud.backup_restore as br

    original = br.BACKUP_SOURCE_OWNER
    br.BACKUP_SOURCE_OWNER = subprocess.run(
        ["id", "-un"], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        cmd = backup_dir_cmd(str(link), str(dest), "data")
    finally:
        br.BACKUP_SOURCE_OWNER = original
    result = _sh(cmd)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in dest.iterdir()) == [".hidden", "warehouse.duckdb"]
    assert [p.name for p in dest.parent.iterdir()] == ["data"], "temporary copy left behind"


@pytest.mark.unit
def test_restore_dir_command_copies_without_following_links() -> None:
    cmd = restore_dir_cmd("/stage/notebooks", "/p/notebooks")
    assert "cp -RP " in cmd and "cp -r " not in cmd
    assert "[ ! -L /stage/notebooks ]" in cmd


@pytest.mark.unit
def test_scheduled_file_copy_refuses_a_link(tmp_path: Path) -> None:
    from dango.platform.cloud.scheduled_backup import _copy_file_without_special_bits

    target = tmp_path / "t"
    target.write_text("x")
    link = tmp_path / "l"
    link.symlink_to(target)
    with pytest.raises(OSError):
        _copy_file_without_special_bits(str(link), str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


@pytest.mark.unit
def test_scheduled_file_copy_keeps_content_and_mode(tmp_path: Path) -> None:
    from dango.platform.cloud.scheduled_backup import _copy_file_without_special_bits

    src = tmp_path / "s"
    src.write_text("x")
    os.chmod(src, 0o640)
    out = tmp_path / "out"
    _copy_file_without_special_bits(str(src), str(out))
    assert out.read_text() == "x" and (out.stat().st_mode & 0o777) == 0o640


@pytest.mark.unit
def test_scheduled_tree_copy_refuses_a_linked_root(tmp_path: Path) -> None:
    from dango.platform.cloud.scheduled_backup import _copy_tree_regular

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s")
    link = tmp_path / "notebooks"
    link.symlink_to(outside, target_is_directory=True)
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(CloudProvisioningError):
        _copy_tree_regular(link, dest, step="copy_dirs", skipped=[], label="notebooks")
    assert not list(dest.iterdir())
