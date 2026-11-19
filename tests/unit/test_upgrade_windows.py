"""tests/unit/test_upgrade_windows.py

dango upgrade: guidance for a locked dango.exe on Windows, access errors, and certificate failures from pip.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.main import cli

pytestmark = pytest.mark.unit

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _project(tmp_path: Path) -> Path:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir(exist_ok=True)
    (dango_dir / "project.yml").write_text(
        "project:\n  name: t\n  version: '1.0'\n  created_by: x\n  purpose: y\n", encoding="utf-8"
    )
    return tmp_path


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    pip: subprocess.CompletedProcess[str],
) -> tuple[int, str, list[Any]]:
    root = _project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    calls: list[Any] = []

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(list(argv))
        if "install" in argv:
            return pip
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with (
        patch("subprocess.run", run),
        patch("dango.cli.commands.upgrade._pre_upgrade_safety_copy", return_value=root / "b"),
        patch("dango.cli.commands.upgrade.shutil.disk_usage", return_value=MagicMock(free=10**10)),
    ):
        result = CliRunner().invoke(cli, ["upgrade", "--version", "99.0.0", "--yes"])
    return result.exit_code, _ANSI.sub("", result.output), calls


def _failed(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["pip"], 1, stdout="", stderr=stderr)


_WINERROR = (
    "ERROR: Could not install packages due to an OSError: [WinError 5] Access is denied: "
    "'C:\\\\Users\\\\me\\\\venv\\\\Scripts\\\\dango.exe'"
)


def test_pip_runs_through_the_same_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = subprocess.CompletedProcess(["pip"], 0, stdout="", stderr="")
    _, _, calls = _run(tmp_path, monkeypatch, "win32", ok)
    install = [c for c in calls if "install" in c][0]
    assert install[:3] == [sys.executable, "-m", "pip"]


def test_windows_prints_the_locked_launcher_note_before_pip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = subprocess.CompletedProcess(["pip"], 0, stdout="", stderr="")
    _, output, _ = _run(tmp_path, monkeypatch, "win32", ok)
    flat = "".join(output.split())
    assert "Access is denied" in output
    assert f"{sys.executable}-mpipinstallgetdango==99.0.0".replace(" ", "") in flat
    assert "--upgrade" not in output
    assert output.index("Access is denied") < output.index("Installing getdango")


def test_other_platforms_do_not_get_the_windows_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = subprocess.CompletedProcess(["pip"], 0, stdout="", stderr="")
    for platform in ("darwin", "linux"):
        _, output, _ = _run(tmp_path, monkeypatch, platform, ok)
        assert "Access is denied" not in output


@pytest.mark.parametrize("text", [_WINERROR, "OSError: [WinError 32] used by another process"])
def test_windows_access_failure_prints_manual_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    code, output, _ = _run(tmp_path, monkeypatch, "win32", _failed(text))
    assert code == 1, output
    flat = "".join(output.split())
    assert "dango stop" in output
    assert f"{sys.executable}-mpipinstallgetdango==99.0.0".replace(" ", "") in flat
    assert "Marimo" in output
    assert "dango start" in output


def test_posix_permission_failure_names_the_permission_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, output, _ = _run(
        tmp_path,
        monkeypatch,
        "linux",
        _failed(
            "ERROR: Could not install packages due to an OSError: [Errno 13] Permission denied"
        ),
    )
    assert code == 1, output
    assert "pip could not write" in output
    assert "Marimo" not in output


def test_unrelated_pip_failure_has_no_extra_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, output, _ = _run(
        tmp_path, monkeypatch, "win32", _failed("ERROR: No matching distribution found")
    )
    assert code == 1
    assert "pip install failed" in output
    assert "Marimo" not in output and "REQUESTS_CA_BUNDLE" not in output


def test_pip_certificate_failure_prints_certificate_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, output, _ = _run(
        tmp_path,
        monkeypatch,
        "linux",
        _failed("SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]'))"),
    )
    assert code == 1
    assert "REQUESTS_CA_BUNDLE" in output and "--cert" in output
    assert "--trusted-host" not in output


def test_pip_output_is_not_parsed_as_markup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, output, _ = _run(tmp_path, monkeypatch, "linux", _failed("boom [/oops] [bold]x"))
    assert code == 1
    assert "[/oops]" in output and "[bold]x" in output
