"""tests/unit/test_validate_dbt_cli_check.py

`dango validate` proves the dbt CLI starts without any network request (`dbt --version` asks
PyPI for the latest versions): the row shows the installed dbt-core version from local metadata.
"""

from __future__ import annotations

import subprocess
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from dango.cli.validate import ProjectValidator

pytestmark = pytest.mark.unit

HELP_OUT = "Usage: dbt [OPTIONS] COMMAND [ARGS]...\n"


def _row(tmp_path: Path, fake_run: Any) -> Any:
    validator = ProjectValidator(tmp_path)
    with (
        patch("dango.transformation._get_dbt_executable", return_value="dbt"),
        patch("subprocess.run", side_effect=fake_run),
    ):
        validator._check_dependencies()
    return next(r for r in validator.results if r.name == "CLI: dbt")


def _ok(cmd: list[str], *a: Any, **k: Any) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=HELP_OUT, stderr="")


def test_the_dbt_check_runs_help_never_version(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], *a: Any, **k: Any) -> SimpleNamespace:
        seen.append(list(cmd))
        return _ok(cmd)

    _row(tmp_path, fake_run)
    dbt_calls = [c for c in seen if c[0] == "dbt"]
    assert dbt_calls == [["dbt", "--help"]]


def test_the_version_line_comes_from_local_metadata(tmp_path: Path) -> None:
    row = _row(tmp_path, _ok)
    assert row.status == "pass"
    assert row.message == f"dbt-core {metadata.version('dbt-core')}"


def test_a_missing_dbt_core_package_still_passes_when_the_cli_starts(tmp_path: Path) -> None:
    def gone(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    with patch("importlib.metadata.version", side_effect=gone):
        row = _row(tmp_path, _ok)
    assert row.status == "pass"
    assert "dbt" in row.message


def test_a_dbt_that_cannot_start_fails(tmp_path: Path) -> None:
    def missing(cmd: list[str], *a: Any, **k: Any) -> SimpleNamespace:
        raise FileNotFoundError("dbt")

    def broken(cmd: list[str], *a: Any, **k: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=2, stdout="", stderr="boom")

    assert "not found" in _row(tmp_path, missing).message
    assert _row(tmp_path, missing).status == "fail"
    assert _row(tmp_path, broken).status == "fail"


def test_a_hanging_dbt_times_out_with_the_command_named(tmp_path: Path) -> None:
    def hang(cmd: list[str], *a: Any, **k: Any) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd, k.get("timeout", 0))

    row = _row(tmp_path, hang)
    assert row.status == "fail"
    assert "dbt --help timed out after 30" in row.message
