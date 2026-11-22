"""tests/unit/test_dbt_executable_lookup.py

Both inline dbt lookups (dango init docs generation, web dbt-run task) pick the platform's venv dbt.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from dango.cli.init import ProjectInitializer
from dango.web.routes.dbt import run_dbt_model_task


class _OsWithName:
    """The real os module, except for a chosen os.name.

    Assigning os.name = "nt" on a POSIX host makes pathlib build WindowsPath objects, so the
    name is faked only inside dango.transformation, where the lookup helper reads it.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(os, attr)


def _make_interpreter_dir(tmp_path: Path, *, with_files: tuple[str, ...]) -> Path:
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("")
    for name in with_files:
        (bin_dir / name).write_text("")
    return bin_dir


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Configure a fake interpreter dir, os.name and an empty PATH; return the configurator."""

    def configure(os_name: str, *, with_files: tuple[str, ...]) -> Path:
        bin_dir = _make_interpreter_dir(tmp_path, with_files=with_files)
        monkeypatch.setattr("sys.executable", str(bin_dir / "python"))
        monkeypatch.setattr("dango.transformation.os", _OsWithName(os_name))
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        return bin_dir

    return configure


def _init_argv(project_dir: Path) -> list[str]:
    """argv that ProjectInitializer._generate_dbt_docs passes to subprocess.run."""
    (project_dir / "dbt").mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *_a: object, **_k: object) -> SimpleNamespace:
        calls.append(list(cmd))
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ProjectInitializer(project_dir)._generate_dbt_docs()

    assert len(calls) == 1
    assert calls[0][1:3] == ["docs", "generate"]
    return calls[0]


def _web_argv(project_root: Path) -> list[str]:
    """argv that run_dbt_model_task passes to subprocess.run."""
    (project_root / "dbt").mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *_a: object, **_k: object) -> SimpleNamespace:
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch("dango.web.routes.dbt.get_project_root", return_value=project_root),
        patch("dango.web.routes.dbt.subprocess.run", side_effect=fake_run),
        patch("dango.web.routes.dbt.ws_manager.broadcast", new_callable=AsyncMock),
        patch("dango.utils.activity_log.log_activity"),
        patch("dango.utils.dbt_status.update_model_status"),
        patch(
            "dango.visualization.metabase.refresh_metabase_connection",
            return_value=(True, None, None),
        ),
    ):
        asyncio.run(run_dbt_model_task("my_model", cascade=False))

    assert len(calls) == 1
    assert calls[0][1:3] == ["run", "--select"]
    return calls[0]


SITES = pytest.mark.parametrize("argv_for", [_init_argv, _web_argv], ids=["init-docs", "web-run"])


@pytest.mark.unit
class TestDbtLookupBehaviour:
    @SITES
    def test_windows_picks_dbt_exe(self, argv_for: Any, host: Any, tmp_path: Path) -> None:
        bin_dir = host("nt", with_files=("dbt.exe",))
        assert argv_for(tmp_path / "proj")[0] == str(bin_dir / "dbt.exe")

    @SITES
    def test_posix_picks_dbt(self, argv_for: Any, host: Any, tmp_path: Path) -> None:
        bin_dir = host("posix", with_files=("dbt",))
        assert argv_for(tmp_path / "proj")[0] == str(bin_dir / "dbt")

    @SITES
    def test_falls_back_to_path_lookup(self, argv_for: Any, host: Any, tmp_path: Path) -> None:
        host("posix", with_files=())
        with patch("shutil.which", return_value="/x/dbt"):
            assert argv_for(tmp_path / "proj")[0] == "/x/dbt"

    @SITES
    def test_last_resort_is_bare_dbt(self, argv_for: Any, host: Any, tmp_path: Path) -> None:
        host("posix", with_files=())
        with patch("shutil.which", return_value=None):
            assert argv_for(tmp_path / "proj")[0] == "dbt"
