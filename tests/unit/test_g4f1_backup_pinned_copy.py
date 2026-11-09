"""tests/unit/test_g4f1_backup_pinned_copy.py

I8-04: the root backup copy is pinned to the directories it opened.

A path checked with ``O_NOFOLLOW`` and then reopened by name can be redirected by a
directory swapped for a link in between.  Each test swaps a directory for a link to a
root-only target at the moment the copy is between its check and its read, and expects
the target's file never to reach the staging directory.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dango.exceptions import CloudProvisioningError

_SB = "dango.platform.cloud.scheduled_backup"


def _project(tmp_path: Path) -> tuple[Path, Path]:
    """A project with ``notebooks/{keep.txt,sub/inner.txt}`` and an outside target directory."""
    project = tmp_path / "project"
    notebooks = project / "notebooks"
    (notebooks / "sub").mkdir(parents=True)
    (notebooks / "keep.txt").write_text("keep")
    (notebooks / "sub" / "inner.txt").write_text("inner")
    outside = tmp_path / "root-only"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (outside / "sub").mkdir()
    (outside / "sub" / "secret.txt").write_text("secret")
    return project, outside


def _swap_for_link(directory: Path, target: Path) -> None:
    directory.rename(directory.with_name(directory.name + ".moved"))
    directory.symlink_to(target, target_is_directory=True)


def _after_use(
    monkeypatch: pytest.MonkeyPatch, trigger: Callable[[Any], bool], swap: Callable[[], None]
) -> None:
    """Run *swap* once, right after the first ``os.open``/``os.stat``/``os.lstat`` call that *trigger* accepts."""
    done: list[bool] = []

    def wrap(real: Callable[..., Any]) -> Callable[..., Any]:
        def inner(path: Any, *args: Any, **kwargs: Any) -> Any:
            result = real(path, *args, **kwargs)
            if not done and trigger(path):
                done.append(True)
                swap()
            return result

        return inner

    for name in ("open", "stat", "lstat"):
        monkeypatch.setattr(os, name, wrap(getattr(os, name)))


def _secrets_in(dest: Path) -> list[Path]:
    return [p for p in dest.rglob("*") if p.is_file() and p.read_text() == "secret"]


@pytest.mark.unit
class TestDirectoryPinnedAfterTheCheck:
    def test_top_directory_swapped_after_it_is_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango.platform.cloud.scheduled_backup import _copy_tree_regular

        project, outside = _project(tmp_path)
        src = project / "notebooks"
        dest = tmp_path / "staging"
        dest.mkdir()
        monkeypatch.setattr(f"{_SB}.PROJECT_DIR", project)
        _after_use(monkeypatch, lambda p: p == src, lambda: _swap_for_link(src, outside))

        _copy_tree_regular(src, dest, step="copy_dirs", left_out=[], label="notebooks")

        assert _secrets_in(dest) == []
        assert (dest / "keep.txt").read_text() == "keep"
        assert (dest / "sub" / "inner.txt").read_text() == "inner"

    def test_nested_directory_swapped_after_it_is_listed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango.platform.cloud.scheduled_backup import _copy_tree_regular

        project, outside = _project(tmp_path)
        src = project / "notebooks"
        sub = src / "sub"
        dest = tmp_path / "staging"
        dest.mkdir()
        monkeypatch.setattr(f"{_SB}.PROJECT_DIR", project)
        _after_use(
            monkeypatch,
            lambda p: os.fspath(p).endswith("sub") and not os.fspath(p).endswith(".moved"),
            lambda: _swap_for_link(sub, outside / "sub"),
        )

        try:
            _copy_tree_regular(src, dest, step="copy_dirs", left_out=[], label="notebooks")
        except CloudProvisioningError:
            pass  # refusing the swapped directory is as good as copying the original
        assert _secrets_in(dest) == []

    def test_parent_of_the_directory_is_a_link(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _copy_tree_regular

        project, outside = _project(tmp_path)
        (project / "dbt").symlink_to(outside, target_is_directory=True)
        (outside / "models").mkdir()
        (outside / "models" / "m.sql").write_text("secret")
        dest = tmp_path / "staging"
        dest.mkdir()
        import dango.platform.cloud.scheduled_backup as sb

        original = sb.PROJECT_DIR
        sb.PROJECT_DIR = project
        try:
            with pytest.raises(CloudProvisioningError):
                _copy_tree_regular(
                    project / "dbt" / "models", dest, step="copy_dirs", left_out=[], label="m"
                )
        finally:
            sb.PROJECT_DIR = original
        assert _secrets_in(dest) == []


@pytest.mark.unit
class TestFileParentsAreNotFollowed:
    def test_file_under_a_swapped_parent_directory_is_refused(self, tmp_path: Path) -> None:
        import dango.platform.cloud.scheduled_backup as sb

        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "root-only"
        outside.mkdir()
        (outside / "auth.yml").write_text("secret")
        (project / ".dango").symlink_to(outside, target_is_directory=True)
        dest = tmp_path / "out.yml"

        original = sb.PROJECT_DIR
        sb.PROJECT_DIR = project
        try:
            with pytest.raises(CloudProvisioningError):
                sb._copy_keeping_modes(project / ".dango" / "auth.yml", dest, step="copy_files")
        finally:
            sb.PROJECT_DIR = original
        assert not dest.exists()


@pytest.mark.unit
class TestCopyKeepsContentAndModes:
    def test_regular_tree_is_copied_with_modes_and_links_are_left_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango.platform.cloud.scheduled_backup import _copy_tree_regular

        project, outside = _project(tmp_path)
        src = project / "notebooks"
        (src / "link.txt").symlink_to(outside / "secret.txt")
        (src / "tool.sh").write_text("x")
        (src / "tool.sh").chmod(0o4755)
        (src / "wal.wal").write_text("e")
        dest = tmp_path / "staging"
        dest.mkdir()
        monkeypatch.setattr(f"{_SB}.PROJECT_DIR", project)
        left_out: list[str] = []

        _copy_tree_regular(src, dest, step="copy_dirs", left_out=left_out, label="notebooks")

        assert left_out == ["notebooks/link.txt"]
        assert not (dest / "link.txt").exists()
        assert not (dest / "wal.wal").exists()
        assert (dest / "tool.sh").stat().st_mode & 0o7777 == 0o755
        assert (dest / "sub" / "inner.txt").read_text() == "inner"
