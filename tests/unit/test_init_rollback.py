"""tests/unit/test_init_rollback.py

A failed or cancelled `dango init` removes only what that run created, never the user's files.
"""

from pathlib import Path
from typing import Any

import pytest

from dango.cli.init import ProjectInitializer

pytestmark = pytest.mark.unit

SENTINELS = {
    "README.md": b"# my own readme\r\nkeep me\n",
    ".gitignore": b"node_modules/\n*.log",
    "data/keep.txt": b"user data \xe2\x9c\x93\n",
    "dbt/keep.sql": b"select 1 as keep\n",
    "notes/todo.txt": b"unrelated\n",
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("DANGO_TELEMETRY", "0")
    monkeypatch.setattr("dango.utils.driver.reuse_verified_driver", lambda plugins_dir: True)
    monkeypatch.setattr(ProjectInitializer, "_generate_dbt_docs", lambda self: True)


def _populate(project: Path) -> None:
    for rel, data in SENTINELS.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _tree(project: Path) -> set[str]:
    return {p.relative_to(project).as_posix() for p in project.rglob("*")}


def _fail_with(monkeypatch: pytest.MonkeyPatch, how: str) -> type[BaseException]:
    if how == "docs":
        monkeypatch.setattr(ProjectInitializer, "_generate_dbt_docs", lambda self: False)
        return SystemExit

    exc: type[BaseException] = KeyboardInterrupt if how == "interrupt" else RuntimeError

    def boom(self: Any, *args: Any, **kwargs: Any) -> bool:
        raise exc("stop")

    monkeypatch.setattr(ProjectInitializer, "_setup_auth", boom)
    # init turns a cancel into SystemExit(1) and re-raises any other error
    return SystemExit if how == "interrupt" else exc


@pytest.mark.parametrize("how", ["docs", "error", "interrupt"])
def test_failed_init_in_existing_directory_keeps_user_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _populate(project)
    before = _tree(project)
    expected = _fail_with(monkeypatch, how)

    with pytest.raises(expected):
        ProjectInitializer(project).initialize(skip_wizard=True)

    for rel, data in SENTINELS.items():
        assert (project / rel).read_bytes() == data, rel
    assert _tree(project) == before


@pytest.mark.parametrize("how", ["docs", "error", "interrupt"])
def test_failed_init_in_empty_directory_leaves_it_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    expected = _fail_with(monkeypatch, how)

    with pytest.raises(expected):
        ProjectInitializer(project).initialize(skip_wizard=True)

    assert _tree(project) == set()


def _raise_after(monkeypatch: pytest.MonkeyPatch, step: str, action: Any) -> None:
    """Run the real init step, then `action(project_dir)`, then fail."""
    real = getattr(ProjectInitializer, step)

    def wrapper(self: ProjectInitializer, *args: Any, **kwargs: Any) -> Any:
        real(self, *args, **kwargs)
        action(self.project_dir)
        raise RuntimeError("stop")

    monkeypatch.setattr(ProjectInitializer, step, wrapper)


def test_rollback_leaves_files_changed_by_other_programs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "notes.md").write_bytes(b"mine\n")
    (project / "init.log").write_bytes(b"")

    def other_program(root: Path) -> None:
        (root / "out.log").write_bytes(b"progress\nreason\n")
        (root / "notes.md").write_bytes(b"mine\nedited meanwhile\n")
        (root / "init.log").write_bytes(b"captured output\n")
        (root / "data" / "user_upload.csv").write_bytes(b"a,b\n")

    _raise_after(monkeypatch, "_create_readme", other_program)

    with pytest.raises(RuntimeError):
        ProjectInitializer(project).initialize(skip_wizard=True)

    assert (project / "out.log").read_bytes() == b"progress\nreason\n"
    assert (project / "notes.md").read_bytes() == b"mine\nedited meanwhile\n"
    assert (project / "init.log").read_bytes() == b"captured output\n"
    assert not (project / "dbt").exists()


def test_snapshot_reads_only_files_init_may_modify(tmp_path: Path) -> None:
    from dango.cli.init_rollback import take_snapshot

    (tmp_path / "notes.md").write_bytes(b"x")
    (tmp_path / ".gitignore").write_bytes(b"y")

    snapshot = take_snapshot(tmp_path)

    assert set(snapshot.contents) == {".gitignore"}
    assert {"notes.md", ".gitignore"} <= snapshot.top_level


def test_rollback_restores_file_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import stat

    project = tmp_path / "proj"
    project.mkdir()
    script = project / "entrypoint.sh"
    script.write_bytes(b"#!/bin/sh\necho mine\n")
    os.chmod(script, 0o644)
    mode_before = stat.S_IMODE(script.stat().st_mode)
    _fail_with(monkeypatch, "error")

    with pytest.raises(RuntimeError):
        ProjectInitializer(project).initialize(skip_wizard=True)

    assert script.read_bytes() == b"#!/bin/sh\necho mine\n"
    assert stat.S_IMODE(script.stat().st_mode) == mode_before


def test_snapshot_size_limit_is_inclusive(tmp_path: Path) -> None:
    from dango.cli.init_rollback import MAX_RESTORE_BYTES, take_snapshot

    (tmp_path / "README.md").write_bytes(b"a" * MAX_RESTORE_BYTES)
    (tmp_path / ".gitignore").write_bytes(b"a" * (MAX_RESTORE_BYTES + 1))

    snapshot = take_snapshot(tmp_path)

    assert "README.md" in snapshot.contents
    assert ".gitignore" not in snapshot.contents


def test_rollback_without_snapshot_removes_nothing(tmp_path: Path) -> None:
    _populate(tmp_path)
    (tmp_path / ".dango").mkdir()
    before = _tree(tmp_path)

    ProjectInitializer(tmp_path)._rollback_initialization()

    assert _tree(tmp_path) == before
    for rel, data in SENTINELS.items():
        assert (tmp_path / rel).read_bytes() == data


def _link_dir(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.fail("symlinks unavailable on this platform")


def test_rollback_does_not_follow_symlinked_write_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "mine.csv").write_bytes(b"keep\n")
    project = tmp_path / "proj"
    project.mkdir()
    _link_dir(project / "data", outside)
    _fail_with(monkeypatch, "error")

    with pytest.raises(RuntimeError):
        ProjectInitializer(project).initialize(skip_wizard=True)

    assert (outside / "mine.csv").read_bytes() == b"keep\n"
    assert (project / "data").is_symlink()


def test_rollback_does_not_write_through_symlinked_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "gi").write_bytes(b"outside rules\n")
    project = tmp_path / "proj"
    project.mkdir()
    _link_dir(project / ".gitignore", outside / "gi")
    _fail_with(monkeypatch, "error")

    with pytest.raises(RuntimeError):
        ProjectInitializer(project).initialize(skip_wizard=True)

    assert (project / ".gitignore").is_symlink()
    content = (outside / "gi").read_bytes()
    # init appended through the link; rollback does not write back through a link
    assert content.startswith(b"outside rules\n")
    assert b"# Dango" in content


def test_rollback_removes_new_symlink_without_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "mine.csv").write_bytes(b"keep\n")
    project = tmp_path / "proj"
    project.mkdir()

    def make_link(root: Path) -> None:
        _link_dir(root / "metabase-plugins", outside)

    _raise_after(monkeypatch, "_create_readme", make_link)

    with pytest.raises(RuntimeError):
        ProjectInitializer(project).initialize(skip_wizard=True)

    assert (outside / "mine.csv").read_bytes() == b"keep\n"
    assert not (project / "metabase-plugins").is_symlink()
    assert _tree(project) == set()
