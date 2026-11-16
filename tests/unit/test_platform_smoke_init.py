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

_MAKE_PROJECT = """
import os
os.makedirs(".dango", exist_ok=True)
open(os.path.join(".dango", "project.yml"), "w", encoding="utf-8").write("project: x\\n")
"""
_MAKE_AUTH = """
open(os.path.join(".dango", "auth.yml"), "w", encoding="utf-8").write("enabled: true\\n")
"""

FAKE_OK = _MAKE_PROJECT + _MAKE_AUTH + 'print("fake dango init ok")\n'

# Real tracebacks go to stderr, so the fakes do the same.
FAKE_TRACEBACK = """
import sys
print("Traceback (most recent call last):", file=sys.stderr)
print("UnicodeEncodeError: 'charmap' codec can't encode character", file=sys.stderr)
sys.exit(1)
"""

FAKE_NO_PROJECT = 'print("fake dango init did nothing")\n'

FAKE_TRACEBACK_EXIT0 = (
    "import sys\n"
    + _MAKE_PROJECT
    + _MAKE_AUTH
    + 'print("Traceback (most recent call last):", file=sys.stderr)\n'
)

# Exit 1 with no traceback line, but project.yml (and auth.yml) were already written.
FAKE_EXIT1_NO_TRACEBACK = (
    "import sys\n"
    + _MAKE_PROJECT
    + _MAKE_AUTH
    + 'print("Error: dbt command not found", file=sys.stderr)\nsys.exit(1)\n'
)

# Exit 0 and project.yml exists, but auth setup did not run (no auth.yml).
FAKE_NO_AUTH = _MAKE_PROJECT + 'print("Skipping auth setup.")\n'

# Exit 0 with both files, but init reported it finished with warnings.
FAKE_WARNINGS = _MAKE_PROJECT + _MAKE_AUTH + 'print("Initialization Completed with Warnings")\n'

# Prints, then outlives a short timeout.
FAKE_SLOW = """
import sys, time
print("fake init started", flush=True)
print("fake init stderr line", file=sys.stderr, flush=True)
time.sleep(4)
"""

# Non-ASCII message on stderr, written as UTF-8 bytes, exit 1.
FAKE_NON_ASCII = """
import sys
sys.stderr.buffer.write("Error: caf\\u00e9 \\U0001f361\\n".encode("utf-8"))
sys.exit(1)
"""

FAKE_STDERR_ONLY_FAIL = """
import sys
print("only on stderr: boom", file=sys.stderr)
sys.exit(1)
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


def _run(
    exe: Path,
    workdir: Path,
    label: str = "utf8",
    extra: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
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
            *extra,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )


def test_pass_when_init_succeeds_and_creates_project_files(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_OK)
    workdir = tmp_path / "work"
    result = _run(exe, workdir, "utf8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS utf8" in result.stdout
    assert (workdir / ".dango" / "project.yml").exists()


def test_pass_prints_tail_of_init_output(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_OK)
    result = _run(exe, tmp_path / "work", "utf8")
    assert result.returncode == 0
    assert "fake dango init ok" in result.stdout


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


def test_fail_on_exit_one_without_traceback_even_if_project_yml_exists(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_EXIT1_NO_TRACEBACK)
    workdir = tmp_path / "work"
    result = _run(exe, workdir, "utf8")
    assert (workdir / ".dango" / "project.yml").exists()
    assert result.returncode == 1
    assert "FAIL utf8" in result.stdout
    assert "exit code 1" in result.stdout


def test_fail_when_auth_yml_missing(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_NO_AUTH)
    result = _run(exe, tmp_path / "work", "utf8")
    assert result.returncode == 1
    assert "FAIL utf8" in result.stdout
    assert "auth.yml" in result.stdout


def test_fail_when_init_completed_with_warnings(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_WARNINGS)
    result = _run(exe, tmp_path / "work", "utf8")
    assert result.returncode == 1
    assert "FAIL utf8" in result.stdout
    assert "warnings" in result.stdout.lower()


def test_timeout_reports_decoded_output_of_both_streams(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_SLOW)
    result = _run(exe, tmp_path / "work", "slow", extra=("--timeout", "1"))
    assert result.returncode == 1
    assert "FAIL slow" in result.stdout
    assert "timed out" in result.stdout
    assert "fake init started" in result.stdout
    assert "fake init stderr line" in result.stdout
    assert "b'" not in result.stdout


def test_failure_output_only_on_stderr_is_reported(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_STDERR_ONLY_FAIL)
    result = _run(exe, tmp_path / "work", "utf8")
    assert result.returncode == 1
    assert "only on stderr: boom" in result.stdout


def test_non_ascii_output_and_label_are_escaped_under_cp1252_stdout(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_NON_ASCII)
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    result = _run(exe, tmp_path / "work", "café", env=env)
    assert result.returncode == 1
    assert "FAIL caf\\xe9" in result.stdout
    assert "Error: caf\\xe9" in result.stdout
    assert "Traceback" not in result.stderr


# Prints without flushing, then outlives a short timeout: only an unbuffered child keeps it.
FAKE_UNFLUSHED = """
import time
print("unflushed marker line")
time.sleep(4)
"""


def test_timeout_keeps_output_the_child_had_not_flushed(tmp_path: Path) -> None:
    exe = _make_fake_dango(tmp_path, FAKE_UNFLUSHED)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    result = _run(exe, tmp_path / "work", "slow", extra=("--timeout", "1"), env=env)
    assert result.returncode == 1
    assert "timed out" in result.stdout
    assert "unflushed marker line" in result.stdout


def test_missing_dango_path_is_a_clean_fail(tmp_path: Path) -> None:
    result = _run(tmp_path / "nope" / "dango", tmp_path / "work", "missing")
    assert result.returncode == 1
    assert "FAIL missing" in result.stdout
    assert "PASS" not in result.stdout
    assert "Traceback" not in result.stderr


def test_non_executable_dango_path_is_a_clean_fail(tmp_path: Path) -> None:
    not_exe = tmp_path / "dango"
    not_exe.write_text("not a program\n", encoding="utf-8")
    not_exe.chmod(0o644)
    result = _run(not_exe, tmp_path / "work", "noexec")
    assert result.returncode == 1
    assert "FAIL noexec" in result.stdout
    assert "PASS" not in result.stdout
    assert "Traceback" not in result.stderr
