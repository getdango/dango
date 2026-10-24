"""tests/unit/test_notebook_symlink_safety.py

Notebook file operations must never follow symlinks (real symlinks in a tmp dir).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

import dango.notebooks as notebooks_pkg
from dango.notebooks.locking import copy_locked_notebook
from dango.utils.dango_db import connect
from dango.web.routes.notebooks import _create_notebook_blocking, _delete_notebook_blocking

SECRET = "SECRET=hunter2\n"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """Project root with a notebooks dir, initialised dango db and an outside secret."""
    (tmp_path / "notebooks").mkdir()
    (tmp_path / ".env").write_text(SECRET)
    with connect(tmp_path):
        pass
    return tmp_path


def _copies(project: Path) -> list[Path]:
    return [p for p in (project / "notebooks").iterdir() if "_copy_" in p.name]


@pytest.mark.unit
class TestCopyLockedNotebook:
    def test_symlink_source_refused_and_content_not_copied(self, project: Path) -> None:
        nb = project / "notebooks"
        os.symlink(project / ".env", nb / "evil.py")
        with pytest.raises(FileNotFoundError):
            copy_locked_notebook(project, "evil", "bob")
        assert _copies(project) == []

    def test_fifo_source_refused(self, project: Path) -> None:
        os.mkfifo(project / "notebooks" / "pipe.py")
        with pytest.raises(FileNotFoundError):
            copy_locked_notebook(project, "pipe", "bob")
        assert _copies(project) == []

    def test_symlinked_directory_component_refused(self, project: Path) -> None:
        outside = project / "outside"
        outside.mkdir()
        (outside / "x.py").write_text(SECRET)
        os.symlink(outside, project / "notebooks" / "sub")
        from dango.notebooks.safe_files import UnsafeFileError, copy_regular_file

        with pytest.raises(UnsafeFileError):
            copy_regular_file(
                project / "notebooks" / "sub" / "x.py",
                project / "notebooks" / "copy.py",
                src_root=project / "notebooks",
            )
        with pytest.raises(UnsafeFileError):
            copy_regular_file(
                project / "notebooks" / "ok.py",
                project / "notebooks" / "sub" / "new.py",
                dst_root=project / "notebooks",
            )
        assert not (outside / "new.py").exists()
        assert not (project / "notebooks" / "copy.py").exists()

    def test_regular_copy_is_regular_file_with_mode(self, project: Path) -> None:
        src = project / "notebooks" / "nb.py"
        src.write_text("print(1)\n")
        src.chmod(0o600)
        name = copy_locked_notebook(project, "nb", "bob")
        dst = project / "notebooks" / name
        assert stat.S_ISREG(os.lstat(dst).st_mode)
        assert dst.read_text() == "print(1)\n"
        assert stat.S_IMODE(os.lstat(dst).st_mode) == 0o644

    def test_missing_source_still_not_found(self, project: Path) -> None:
        with pytest.raises(FileNotFoundError):
            copy_locked_notebook(project, "nope", "bob")


@pytest.mark.unit
class TestCreateNotebook:
    def test_dangling_symlink_refused_and_outside_not_created(self, project: Path) -> None:
        outside = project / "scripts_target.py"
        os.symlink(outside, project / "notebooks" / "plant.py")
        with pytest.raises(FileExistsError):
            _create_notebook_blocking(project, "plant", "blank", "a@b.c")
        assert not outside.exists()
        assert os.path.islink(project / "notebooks" / "plant.py")

    def test_symlink_to_existing_file_not_overwritten(self, project: Path) -> None:
        os.symlink(project / ".env", project / "notebooks" / "plant.py")
        with pytest.raises(FileExistsError):
            _create_notebook_blocking(project, "plant", "blank", "a@b.c")
        assert (project / ".env").read_text() == SECRET

    def test_normal_create_is_regular_file_mode_644(self, project: Path) -> None:
        old = os.umask(0o077)
        try:
            _create_notebook_blocking(project, "fresh", "blank", "a@b.c")
        finally:
            os.umask(old)
        target = project / "notebooks" / "fresh.py"
        assert stat.S_ISREG(os.lstat(target).st_mode)
        assert stat.S_IMODE(os.lstat(target).st_mode) == 0o644

    def test_existing_file_still_reports_exists(self, project: Path) -> None:
        (project / "notebooks" / "dup.py").write_text("x")
        with pytest.raises(FileExistsError, match="already exists"):
            _create_notebook_blocking(project, "dup", "blank", "a@b.c")


@pytest.mark.unit
class TestHalfWrittenCleanup:
    def test_failed_copy_removes_destination(self, tmp_path: Path) -> None:
        from dango.notebooks.safe_files import copy_regular_file

        src = tmp_path / "s.py"
        src.write_text("data")
        dst = tmp_path / "d.py"
        with patch("dango.notebooks.safe_files.shutil.copyfileobj", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                copy_regular_file(src, dst)
        assert not dst.exists()


@pytest.mark.unit
class TestDeleteSymlink:
    def test_delete_removes_only_the_link(self, project: Path) -> None:
        os.symlink(project / ".env", project / "notebooks" / "gone.py")
        _delete_notebook_blocking(project, "gone")
        assert not os.path.lexists(project / "notebooks" / "gone.py")
        assert (project / ".env").read_text() == SECRET


@pytest.mark.unit
def test_copy_helpers_not_used_in_file_operation_modules() -> None:
    """copy2/copymode can reset ACL masks; the notebook copy paths must not use them."""
    pkg = Path(notebooks_pkg.__file__).parent
    route = pkg.parent / "web" / "routes" / "notebooks.py"
    for path in (pkg / "safe_files.py", pkg / "locking.py", route):
        text = path.read_text()
        assert "copy2" not in text, path
        assert "copymode" not in text, path
