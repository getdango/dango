"""tests/unit/test_g4f1_r1_backup.py

G4F1 review round 1 (H1, L3): the linked ``data/`` directory is opened once and checked on
that open handle; a tree too deep to copy fails with a clear message.
"""

from __future__ import annotations

import inspect
import os
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from dango.exceptions import CloudProvisioningError

_SB = "dango.platform.cloud.scheduled_backup"


def _archive_names(archive: Path) -> list[str]:
    with tarfile.open(archive) as tar:
        return tar.getnames()


def _create(project: Path, backups: Path) -> Path:
    from dango.platform.cloud.scheduled_backup import _create_local_archive

    with (
        patch(f"{_SB}.PROJECT_DIR", project),
        patch(f"{_SB}.BACKUP_DIR", backups),
        patch(f"{_SB}._stop_services"),
        patch(f"{_SB}._start_services"),
        patch(f"{_SB}._checkpoint_databases", return_value=[]),
        patch(f"{_SB}._get_metabase_volume_path", return_value=None),
    ):
        archive, _manifest, _warnings = _create_local_archive("test")
    return archive


def _owner_is_me() -> Any:
    import dango.platform.cloud.backup_restore as br

    me = os.environ.get("USER") or os.getlogin()
    return patch.object(br, "BACKUP_SOURCE_OWNER", me)


def _hook(monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...], trigger: Any, swap: Any) -> None:
    done: list[bool] = []

    def wrap(real: Callable[..., Any]) -> Callable[..., Any]:
        def inner(path: Any, *args: Any, **kwargs: Any) -> Any:
            result = real(path, *args, **kwargs)
            if not done and trigger(path):
                done.append(True)
                swap()
            return result

        return inner

    for name in names:
        monkeypatch.setattr(os, name, wrap(getattr(os, name)))


@pytest.mark.unit
class TestLinkedDataDirIsOpenedOnce:
    def _layout(self, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        project = tmp_path / "project"
        project.mkdir()
        parent = tmp_path / "home" / "v"
        (parent / "real").mkdir(parents=True)
        (parent / "real" / "keep.txt").write_text("keep")
        outside = tmp_path / "outside"
        (outside / "real").mkdir(parents=True)
        (outside / "real" / "secret.txt").write_text("secret")
        (project / "data").symlink_to(parent / "real", target_is_directory=True)
        return project, parent, outside, tmp_path / "backups"

    def test_parent_of_the_target_swapped_after_the_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project, parent, outside, backups = self._layout(tmp_path)
        real = str(parent / "real")
        link = str(project / "data")

        def swap() -> None:
            parent.rename(parent.with_name("v.moved"))
            parent.symlink_to(outside, target_is_directory=True)

        _hook(monkeypatch, ("open", "stat"), lambda p: os.fspath(p) in (real, link), swap)
        with _owner_is_me():
            archive = _create(project, backups)

        names = _archive_names(archive)
        assert not any(n.endswith("secret.txt") for n in names), names
        assert any(n.endswith("data/keep.txt") for n in names), names

    def test_ownership_is_checked_on_the_opened_directory(self, tmp_path: Path) -> None:
        project, _parent, _outside, backups = self._layout(tmp_path)
        other = SimpleNamespace(pw_uid=os.getuid() + 1)
        with (
            _owner_is_me(),
            patch("pwd.getpwnam", return_value=other),
            pytest.raises(CloudProvisioningError, match="owned by the service user"),
        ):
            _create(project, backups)

    def test_a_linked_directory_owned_by_the_service_user_is_backed_up(
        self, tmp_path: Path
    ) -> None:
        project, _parent, _outside, backups = self._layout(tmp_path)
        with _owner_is_me():
            archive = _create(project, backups)
        assert any(n.endswith("data/keep.txt") for n in _archive_names(archive))


@pytest.mark.unit
class TestDeepTreeFailsClearly:
    def test_recursion_error_becomes_a_clear_failure(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _copy_tree_regular

        src = tmp_path / "notebooks"
        src.mkdir()
        fd = os.open(src, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for _ in range(80):
                os.mkdir("d", dir_fd=fd)
                child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
        finally:
            os.close(fd)
        dest = tmp_path / "staging"
        dest.mkdir()
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(len(inspect.stack()) + 50)
        try:
            with pytest.raises(CloudProvisioningError, match="too deep"):
                _copy_tree_regular(src, dest, step="copy_dirs", left_out=[], label="notebooks")
        finally:
            sys.setrecursionlimit(limit)
