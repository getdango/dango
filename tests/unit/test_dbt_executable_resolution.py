"""tests/unit/test_dbt_executable_resolution.py

Every dbt invocation uses the project's own dbt (beside sys.executable), never a bare PATH lookup.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import dango
from dango.transformation import _get_dbt_executable

DBT_NAME = "dbt.exe" if os.name == "nt" else "dbt"


@pytest.fixture
def venv_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake venv bin dir holding a dbt, with sys.executable inside it and an empty PATH."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / DBT_NAME).write_text("#!/bin/sh\n")
    monkeypatch.setattr("sys.executable", str(bin_dir / "python"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    return bin_dir


def _ok(*_a: object, **_k: object) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.mark.unit
class TestGetDbtExecutable:
    def test_prefers_sibling_of_interpreter(self, venv_bin: Path) -> None:
        assert _get_dbt_executable() == str(venv_bin / DBT_NAME)

    def test_falls_back_to_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "py"
        bin_dir.mkdir()
        monkeypatch.setattr("sys.executable", str(bin_dir / "python"))
        with patch("shutil.which", return_value="/usr/local/bin/dbt"):
            assert _get_dbt_executable() == "/usr/local/bin/dbt"

    def test_last_resort_is_bare_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "py"
        bin_dir.mkdir()
        monkeypatch.setattr("sys.executable", str(bin_dir / "python"))
        with patch("shutil.which", return_value=None):
            assert _get_dbt_executable() == "dbt"


@pytest.mark.unit
class TestCallSitesUseResolvedDbt:
    def test_dango_run_uses_venv_dbt_with_empty_path(self, venv_bin: Path, tmp_path: Path) -> None:
        from dango.cli.main import cli

        project = tmp_path / "proj"
        (project / "dbt").mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *a: object, **k: object) -> SimpleNamespace:
            calls.append(list(cmd))
            return SimpleNamespace(returncode=1)

        with (
            patch("dango.cli.utils.require_project_context", return_value=project),
            patch("subprocess.run", side_effect=fake_run),
        ):
            CliRunner().invoke(cli, ["run"])

        assert calls, "dbt was never invoked"
        assert calls[0][0] == str(venv_bin / DBT_NAME)
        assert calls[0][1] == "build"

    def test_docs_generate_uses_venv_dbt(self, venv_bin: Path, tmp_path: Path) -> None:
        from dango.cli.main import cli

        project = tmp_path / "proj"
        (project / "dbt").mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *a: object, **k: object) -> SimpleNamespace:
            calls.append(list(cmd))
            return SimpleNamespace(returncode=1)

        with (
            patch("dango.cli.utils.require_project_context", return_value=project),
            patch("subprocess.run", side_effect=fake_run),
        ):
            CliRunner().invoke(cli, ["docs", "generate"])

        assert calls
        assert calls[0][0] == str(venv_bin / DBT_NAME)
        assert calls[0][1:3] == ["docs", "generate"]

    def test_validate_parse_and_version_use_venv_dbt(self, venv_bin: Path, tmp_path: Path) -> None:
        from dango.cli.validate import ProjectValidator

        project = tmp_path / "proj"
        (project / "dbt").mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *a: object, **k: object) -> SimpleNamespace:
            calls.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="installed: 1.0\n", stderr="")

        validator = ProjectValidator(project)
        with patch("subprocess.run", side_effect=fake_run):
            validator._validate_dbt_models()
            try:
                validator._check_dependencies()
            except Exception:  # noqa: BLE001 - only the dbt --version call matters here
                pass

        heads = {c[1]: c[0] for c in calls if len(c) > 1 and c[1] in ("parse", "--version")}
        assert heads.get("parse") == str(venv_bin / DBT_NAME)
        assert heads.get("--version") == str(venv_bin / DBT_NAME)

    def test_watcher_command_uses_venv_dbt(self, venv_bin: Path, tmp_path: Path) -> None:
        from dango.platform.local.watcher_runner import run_dbt_command

        project = tmp_path / "proj"
        (project / "dbt").mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *a: object, **k: object) -> SimpleNamespace:
            calls.append(list(cmd))
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            run_dbt_command(project)

        assert calls
        assert calls[0][0] == str(venv_bin / DBT_NAME)
        assert calls[0][1] == "run"


def find_bare_dbt_heads(source: str) -> list[int]:
    """Line numbers of list/tuple literals shaped like a dbt command: ["dbt", "<str>", ...]."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 2:
            continue
        first, second = node.elts[0], node.elts[1]
        if (
            isinstance(first, ast.Constant)
            and first.value == "dbt"
            and isinstance(second, ast.Constant)
            and isinstance(second.value, str)
        ):
            hits.append(node.lineno)
    return hits


@pytest.mark.unit
class TestNoBareDbtCommandHeads:
    def test_scanner_flags_commands_but_not_tags_or_one_element_lists(self) -> None:
        assert find_bare_dbt_heads('cmd = ["dbt", "build"]') == [1]
        assert find_bare_dbt_heads('cmd = [\n    "dbt",\n    "docs",\n]') == [1]
        assert find_bare_dbt_heads('r = APIRouter(tags=["dbt"])') == []
        assert find_bare_dbt_heads('d = [".dango", "dbt", "dbt/models"]') == []

    def test_no_bare_dbt_command_heads(self) -> None:
        root = Path(dango.__file__).parent
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            for line in find_bare_dbt_heads(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(root.parent)}:{line}")
        assert offenders == [], "bare 'dbt' command heads: use _get_dbt_executable()"
