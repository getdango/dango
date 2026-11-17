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
    monkeypatch.setattr(ProjectInitializer, "_setup_metabase", lambda self: True)
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
    return exc


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
