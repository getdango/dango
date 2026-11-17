"""tests/unit/test_validate_dbt_timeout.py

`dango validate` checks `dbt --version`. A slow but working dbt (first run, cold disk, antivirus)
must pass; a dbt that hangs past the limit must still fail with a message that says what happened.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from dango.cli.validate import ProjectValidator

pytestmark = pytest.mark.unit

VERSION_OUT = "Core:\n  - installed: 1.10.20\n"


def _dbt_result(validator: ProjectValidator) -> Any:
    return next(r for r in validator.results if r.name == "CLI: dbt")


def _check(tmp_path: Path, fake_run: Any) -> Any:
    validator = ProjectValidator(tmp_path)
    with (
        patch("dango.transformation._get_dbt_executable", return_value="dbt"),
        patch("subprocess.run", side_effect=fake_run),
    ):
        validator._check_dependencies()
    return _dbt_result(validator)


def _taking(seconds: float) -> Any:
    """A subprocess.run stand-in for a dbt that needs `seconds` to print its version."""

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> SimpleNamespace:
        limit = kwargs.get("timeout")
        if limit is not None and seconds > limit:
            raise subprocess.TimeoutExpired(cmd, limit)
        return SimpleNamespace(returncode=0, stdout=VERSION_OUT, stderr="")

    return fake_run


def test_a_dbt_that_needs_seven_seconds_passes(tmp_path: Path) -> None:
    result = _check(tmp_path, _taking(7))
    assert result.status == "pass"
    assert "installed: 1.10.20" in result.message or "Core" in result.message


def test_a_dbt_that_needs_twenty_five_seconds_passes(tmp_path: Path) -> None:
    assert _check(tmp_path, _taking(25)).status == "pass"


def test_a_dbt_that_hangs_fails_with_an_actionable_message(tmp_path: Path) -> None:
    result = _check(tmp_path, _taking(10_000))
    assert result.status == "fail"
    assert "timed out after 30" in result.message
    assert "first run" in result.message
    assert "yourself" not in result.message  # validate runs the project's own dbt, not PATH's
    assert "dango validate" in result.message
    assert "Error checking dbt" not in result.message
    assert "dbt command failed" not in result.message


def test_the_limit_passed_to_dbt_is_at_least_thirty_seconds(tmp_path: Path) -> None:
    seen: list[Any] = []

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> SimpleNamespace:
        seen.append(kwargs.get("timeout"))
        return SimpleNamespace(returncode=0, stdout=VERSION_OUT, stderr="")

    _check(tmp_path, fake_run)
    assert seen
    assert seen[-1] is not None and seen[-1] >= 30


def test_a_failing_dbt_still_fails_and_a_missing_dbt_is_reported(tmp_path: Path) -> None:
    def bad(cmd: list[str], *a: Any, **k: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    def missing(cmd: list[str], *a: Any, **k: Any) -> SimpleNamespace:
        raise FileNotFoundError("dbt")

    assert _check(tmp_path, bad).status == "fail"
    assert "not found" in _check(tmp_path, missing).message


def test_a_fast_dbt_reports_its_version_line(tmp_path: Path) -> None:
    result = _check(tmp_path, _taking(0.1))
    assert result.status == "pass"
    assert result.message == "Core:"
