"""tests/unit/test_nonutf8_env_startup.py

A stray non-UTF-8 ``.env`` in the working directory must not crash ``dango`` at import time, and a
command run outside a project or from a project subdirectory must not create ``.dango`` there.
Every test runs the real CLI in a child process, because the failure happens during import.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
# "café=1" saved by Notepad on Windows (cp1252): invalid UTF-8.
CP1252_ENV = b"caf\xe9=1\n"


def _run(
    cwd: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in ("DANGO_PROJECT_ROOT", "FASTMCP_ENV_FILE", "VIRTUAL_ENV")
    }
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    env["DANGO_TELEMETRY"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env or {})
    with tempfile.TemporaryDirectory() as home:
        env["HOME"] = home
        env["USERPROFILE"] = home
        return subprocess.run(
            [sys.executable, *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            check=False,
        )


def _cli(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(cwd, "-m", "dango.cli.main", *args)


@pytest.fixture
def cwd_with_cp1252_env(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_bytes(CP1252_ENV)
    return work


class TestStrayNonUtf8EnvInTheWorkingDirectory:
    def test_help_works(self, cwd_with_cp1252_env: Path) -> None:
        done = _cli(cwd_with_cp1252_env, "--help")
        assert "Traceback" not in done.stderr, done.stderr[-1500:]
        assert done.returncode == 0, done.stderr[-1500:]
        assert "Usage:" in done.stdout
        assert "dango init" in done.stdout

    def test_version_works(self, cwd_with_cp1252_env: Path) -> None:
        done = _cli(cwd_with_cp1252_env, "--version")
        assert "Traceback" not in done.stderr, done.stderr[-1500:]
        assert done.returncode == 0, done.stderr[-1500:]
        assert done.stdout.startswith("dango, version ")

    def test_status_outside_a_project_gives_the_normal_message(
        self, cwd_with_cp1252_env: Path
    ) -> None:
        done = _cli(cwd_with_cp1252_env, "status")
        assert "Traceback" not in done.stderr, done.stderr[-1500:]
        assert "UnicodeDecodeError" not in done.stderr
        assert done.returncode == 1
        assert "Not in a Dango project" in done.stdout + done.stderr

    def test_the_stray_file_is_left_untouched(self, cwd_with_cp1252_env: Path) -> None:
        _cli(cwd_with_cp1252_env, "--help")
        assert (cwd_with_cp1252_env / ".env").read_bytes() == CP1252_ENV

    def test_mcp_commands_still_import_fastmcp_and_register_the_server(
        self, cwd_with_cp1252_env: Path
    ) -> None:
        code = (
            "import sys\n"
            "import dango.cli.commands.mcp_server as m\n"
            "print(type(m.mcp).__module__.split('.')[0], type(m.mcp).__name__)\n"
            "print('fastmcp' in sys.modules)\n"
        )
        done = _run(cwd_with_cp1252_env, "-c", code)
        assert "Traceback" not in done.stderr, done.stderr[-1500:]
        assert done.returncode == 0, done.stderr[-1500:]
        assert done.stdout.split() == ["fastmcp", "FastMCP", "True"]

    def test_mcp_help_lists_its_commands(self, cwd_with_cp1252_env: Path) -> None:
        done = _cli(cwd_with_cp1252_env, "mcp", "--help")
        assert "Traceback" not in done.stderr, done.stderr[-1500:]
        assert done.returncode == 0, done.stderr[-1500:]
        assert "run" in done.stdout and "setup" in done.stdout

    def test_the_fastmcp_env_file_setting_is_restored_when_it_was_unset(
        self, cwd_with_cp1252_env: Path
    ) -> None:
        code = (
            "import os\n"
            "import dango.cli.commands.mcp_server\n"
            "print(repr(os.environ.get('FASTMCP_ENV_FILE')))\n"
        )
        done = _run(cwd_with_cp1252_env, "-c", code)
        assert done.returncode == 0, done.stderr[-1500:]
        assert done.stdout.strip() == "None"

    def test_the_fastmcp_env_file_setting_is_restored_when_the_user_set_one(
        self, cwd_with_cp1252_env: Path
    ) -> None:
        code = (
            "import os\n"
            "import dango.cli.commands.mcp_server\n"
            "print(repr(os.environ.get('FASTMCP_ENV_FILE')))\n"
        )
        done = _run(
            cwd_with_cp1252_env, "-c", code, extra_env={"FASTMCP_ENV_FILE": "missing-on-purpose"}
        )
        assert done.returncode == 0, done.stderr[-1500:]
        assert done.stdout.strip() == "'missing-on-purpose'"


@pytest.fixture
def project_with_subdir(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    sub = root / "sub" / "deeper"
    sub.mkdir(parents=True)
    (root / ".dango").mkdir()
    (root / ".dango" / "project.yml").write_text("project:\n  name: x\n", encoding="utf-8")
    return root, sub


class TestNoStrayDotDangoInTheWorkingDirectory:
    def test_command_outside_a_project_creates_no_dot_dango(self, tmp_path: Path) -> None:
        work = tmp_path / "empty"
        work.mkdir()
        done = _cli(work, "status")
        assert done.returncode == 1
        assert "Not in a Dango project" in done.stdout + done.stderr
        assert sorted(p.name for p in work.iterdir()) == []

    def test_subcommand_help_outside_a_project_creates_no_dot_dango(self, tmp_path: Path) -> None:
        work = tmp_path / "empty"
        work.mkdir()
        done = _cli(work, "status", "--help")
        assert done.returncode == 0, done.stderr[-1500:]
        assert sorted(p.name for p in work.iterdir()) == []

    def test_top_level_help_and_version_create_no_dot_dango(self, tmp_path: Path) -> None:
        work = tmp_path / "empty"
        work.mkdir()
        assert _cli(work, "--help").returncode == 0
        assert _cli(work, "--version").returncode == 0
        assert sorted(p.name for p in work.iterdir()) == []

    def test_command_from_a_project_subdirectory_creates_no_dot_dango_there(
        self, project_with_subdir: tuple[Path, Path]
    ) -> None:
        root, sub = project_with_subdir
        done = _cli(sub, "status", "--help")
        assert done.returncode == 0, done.stderr[-1500:]
        assert not (sub / ".dango").exists()
        assert not (root / "sub" / ".dango").exists()

    def test_command_from_a_project_subdirectory_logs_in_the_project(
        self, project_with_subdir: tuple[Path, Path]
    ) -> None:
        root, sub = project_with_subdir
        done = _cli(sub, "status", "--help")
        assert done.returncode == 0, done.stderr[-1500:]
        assert (root / ".dango" / "logs" / "dango.log").is_file()
        assert not (sub / ".dango").exists()

    def test_project_root_from_the_environment_still_decides_the_log_location(
        self, tmp_path: Path
    ) -> None:
        work = tmp_path / "elsewhere"
        work.mkdir()
        root = tmp_path / "server-project"
        root.mkdir()
        done = _run(
            work,
            "-m",
            "dango.cli.main",
            "status",
            "--help",
            extra_env={"DANGO_PROJECT_ROOT": str(root)},
        )
        assert done.returncode == 0, done.stderr[-1500:]
        assert (root / ".dango" / "logs" / "dango.log").is_file()
        assert not (work / ".dango").exists()
