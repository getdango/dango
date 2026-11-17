"""tests/unit/test_dbt_executable_lookup.py

Both dbt call sites (dango init docs generation, web model run) must resolve the dbt
binary beside the interpreter, using dbt.exe on Windows, via one shared helper.
"""

import asyncio
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dango.cli.init import ProjectInitializer
from dango.web.routes.dbt import run_dbt_model_task


class _FakeOs:
    """Stand-in for the os module that reports a different os.name.

    Patching the real os.name would make pathlib build the wrong Path flavour on the
    host, so only dango.transformation's view of os is replaced.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr: str):
        return getattr(os, attr)


def _interpreter_dir(tmp_path: Path, *binaries: str) -> Path:
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("", encoding="utf-8")
    for name in binaries:
        (bin_dir / name).write_text("", encoding="utf-8")
    return bin_dir


def _setup(monkeypatch, bin_dir: Path, os_name: str, which: str | None) -> None:
    monkeypatch.setattr("sys.executable", str(bin_dir / "python"))
    monkeypatch.setattr("dango.transformation.os", _FakeOs(os_name))
    monkeypatch.setattr(shutil, "which", lambda _name: which)


def _init_argv0(tmp_path: Path) -> str:
    project = tmp_path / "proj"
    (project / "dbt").mkdir(parents=True)
    recorder = MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr=""))
    with patch("subprocess.run", recorder):
        ProjectInitializer(project)._generate_dbt_docs()
    return recorder.call_args.args[0][0]


def _route_argv0(tmp_path: Path) -> str:
    project = tmp_path / "proj"
    (project / "dbt").mkdir(parents=True)
    recorder = MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr=""))
    with (
        patch("dango.web.routes.dbt.get_project_root", return_value=project),
        patch("dango.web.routes.dbt.subprocess.run", recorder),
        patch("dango.web.routes.dbt.ws_manager.broadcast", new_callable=AsyncMock),
        patch("dango.utils.activity_log.log_activity"),
    ):
        asyncio.run(run_dbt_model_task("m", cascade=False))
    return recorder.call_args.args[0][0]


CALLERS = [pytest.param(_init_argv0, id="init-docs"), pytest.param(_route_argv0, id="web-route")]


@pytest.mark.parametrize("argv0_of", CALLERS)
class TestDbtExecutableLookup:
    def test_windows_picks_dbt_exe(self, tmp_path, monkeypatch, argv0_of):
        bin_dir = _interpreter_dir(tmp_path, "dbt.exe")
        _setup(monkeypatch, bin_dir, "nt", which=None)
        assert argv0_of(tmp_path) == str(bin_dir / "dbt.exe")

    def test_posix_picks_dbt(self, tmp_path, monkeypatch, argv0_of):
        bin_dir = _interpreter_dir(tmp_path, "dbt")
        _setup(monkeypatch, bin_dir, "posix", which=None)
        assert argv0_of(tmp_path) == str(bin_dir / "dbt")

    def test_falls_back_to_which(self, tmp_path, monkeypatch, argv0_of):
        bin_dir = _interpreter_dir(tmp_path)
        _setup(monkeypatch, bin_dir, "posix", which="/usr/local/bin/dbt")
        assert argv0_of(tmp_path) == "/usr/local/bin/dbt"

    def test_falls_back_to_bare_dbt(self, tmp_path, monkeypatch, argv0_of):
        bin_dir = _interpreter_dir(tmp_path)
        _setup(monkeypatch, bin_dir, "nt", which=None)
        assert argv0_of(tmp_path) == "dbt"
