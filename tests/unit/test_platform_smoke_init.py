"""tests/unit/test_platform_smoke_init.py

scripts/platform_smoke_init.py runs `dango init --skip-wizard` and reports PASS/FAIL per label.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "platform_smoke_init.py"

FAKE_OK = """
import os
print("fake dango init ok")
os.makedirs(".dango", exist_ok=True)
open(os.path.join(".dango", "project.yml"), "w", encoding="utf-8").write("project: x\\n")
"""

FAKE_TRACEBACK = """
import sys
print("Traceback (most recent call last):")
print("UnicodeEncodeError: 'charmap' codec can't encode character")
sys.exit(1)
"""

FAKE_NO_PROJECT = """
print("fake dango init did nothing")
"""

FAKE_TRACEBACK_EXIT0 = """
import os
print("Traceback (most recent call last):")
os.makedirs(".dango", exist_ok=True)
open(os.path.join(".dango", "project.yml"), "w", encoding="utf-8").write("project: x\\n")
"""


def _make_fake_dango(tmp_path: Path, body: str) -> Path:
    """Create an executable that runs `body` with the current interpreter."""
    impl = tmp_path / "fake_dango_impl.py"
    impl.write_text(body, encoding="utf-8")
    if os.name == "nt":
        exe = tmp_path / "fake_dango.cmd"
        exe.write_text(f'@"{sys.executable}" "{impl}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "fake_dango"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n', encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def _run(exe: Path, workdir: Path, label: str = "utf8") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dango",
            str(exe),
            "--workdir",
            str(workdir),
            "--label",
            label,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_pass_when_init_succeeds_and_creates_project_yml(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_OK)
    workdir = tmp_path / "work"
    result = _run(exe, workdir, "utf8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS utf8" in result.stdout
    assert (workdir / ".dango" / "project.yml").exists()


def test_fail_on_nonzero_exit_with_traceback(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_TRACEBACK)
    result = _run(exe, tmp_path / "work", "cp1252")
    assert result.returncode == 1
    assert "FAIL cp1252" in result.stdout
    assert "charmap" in result.stdout


def test_fail_when_project_yml_missing(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_NO_PROJECT)
    result = _run(exe, tmp_path / "work", "utf8")
    assert result.returncode == 1
    assert "FAIL utf8" in result.stdout
    assert "project.yml" in result.stdout


def test_fail_when_traceback_printed_despite_exit_zero(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_TRACEBACK_EXIT0)
    result = _run(exe, tmp_path / "work", "utf8")
    assert result.returncode == 1
    assert "FAIL utf8" in result.stdout
    assert "traceback" in result.stdout.lower()
