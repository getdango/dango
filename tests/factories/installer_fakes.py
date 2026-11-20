"""tests/factories/installer_fakes.py

Runs install.sh against fake python3 and dango executables on an isolated PATH, recording every
python invocation, so tests can assert what the installer would have run. POSIX-only.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install.sh"
BASH = "/bin/bash"  # the installer's shebang interpreter (bash 3.2 on macOS)
TOOLS = ("cut", "tr", "grep", "sed", "uname", "mkdir", "basename", "cat", "sort", "env")

_FAKE_PYTHON = """#!{python}
import json, os, pathlib, sys

args = sys.argv[1:]
with open(os.environ["FAKE_RECORD"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")
if args[:1] == ["-c"]:
    print("3.12")
elif args[:2] == ["-m", "venv"]:
    (pathlib.Path(args[2]) / "bin").mkdir(parents=True, exist_ok=True)
    (pathlib.Path(args[2]) / "bin" / "activate").write_text("", encoding="utf-8")
elif args[:2] == ["-m", "site"]:
    print(os.environ["FAKE_USER_BASE"])
elif "--dry-run" in args and os.environ.get("FAKE_WOULD_UPGRADE"):
    print("Would upgrade: somepkg 1.0 to 2.0")
sys.exit(0)
"""

_FAKE_DANGO = """#!{python}
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_RECORD"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(["dango", *args]) + "\\n")
if args[:1] == ["--version"]:
    print("dango, version 1.0.15")
sys.exit(0)
"""


@dataclass
class InstallRun:
    """Outcome of one installer run: process result, recorded python calls, directories."""

    proc: subprocess.CompletedProcess[str]
    record: Path
    cwd: Path
    home: Path
    user_bin: Path

    @property
    def output(self) -> str:
        """Combined stdout and stderr."""
        return self.proc.stdout + self.proc.stderr

    def calls(self) -> list[list[str]]:
        """Every recorded invocation (python and dango), as argv lists."""
        if not self.record.exists():
            return []
        return [json.loads(ln) for ln in self.record.read_text("utf-8").splitlines()]

    def pip_installs(self) -> list[list[str]]:
        """The recorded `-m pip install ...` invocations."""
        return [c for c in self.calls() if c[:3] == ["-m", "pip", "install"]]


def _write_exe(path: Path, text: str) -> None:
    path.write_text(text.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def make_bin(base: Path, dango_on_path: bool = True) -> tuple[Path, Path]:
    """Build an isolated PATH dir (utilities, fake python3, optional fake dango).

    Returns (path dir, user-base dir whose bin/ holds a second fake dango).
    """
    bindir = base / "bin"
    bindir.mkdir()
    for tool in TOOLS:
        found = shutil.which(tool)
        assert found, tool
        (bindir / tool).symlink_to(found)
    _write_exe(bindir / "python3", _FAKE_PYTHON)
    if dango_on_path:
        _write_exe(bindir / "dango", _FAKE_DANGO)
    user_base = base / "userbase"
    (user_base / "bin").mkdir(parents=True)
    _write_exe(user_base / "bin" / "dango", _FAKE_DANGO)
    return bindir, user_base


def run_installer(
    base: Path,
    env: dict[str, str],
    *,
    dango_on_path: bool = True,
    seed_project: str = "",
    snippet: str = "",
    existing_dirs: tuple[str, ...] = (),
    timeout: float = 60,
) -> InstallRun:
    """Run install.sh (or, with `snippet`, a bash snippet after sourcing it) with no terminal.

    seed_project: "" (new project), "venv" (existing project with a venv) or "novenv".
    """
    bindir, user_base = make_bin(base, dango_on_path)
    cwd = base / "proj"
    cwd.mkdir()
    for name in existing_dirs:
        (cwd / name).mkdir()
    home = base / "home"
    home.mkdir()
    if seed_project:
        (cwd / ".dango").mkdir()
        (cwd / ".dango" / "project.yml").write_text("project: {}\n", encoding="utf-8")
    if seed_project == "venv":
        (cwd / "venv" / "bin").mkdir(parents=True)
        (cwd / "venv" / "bin" / "activate").write_text("", encoding="utf-8")
    record = base / "record.jsonl"
    full_env = {
        "PATH": str(bindir),
        "HOME": str(home),
        "SHELL": "/bin/bash",
        "FAKE_RECORD": str(record),
        "FAKE_USER_BASE": str(user_base),
        **env,
    }
    argv = [BASH, str(INSTALL_SH)]
    if snippet:
        full_env["DANGO_INSTALL_SOURCED"] = "1"
        argv = [BASH, "-c", f'source "{INSTALL_SH}"; PYTHON_CMD=python3; {snippet}']
    proc = subprocess.run(
        argv,
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # no controlling terminal: /dev/tty cannot be opened
        timeout=timeout,
    )
    return InstallRun(proc, record, cwd, home, user_base / "bin")


def base_env(**extra: str) -> dict[str, str]:
    """Environment for a fully non-interactive new-project install (venv, install only)."""
    env = {
        "DANGO_INSTALL_NONINTERACTIVE": "1",
        "DANGO_INSTALL_DOCKER_CONTINUE": "y",
        "DANGO_INSTALL_MODE": "1",
        "DANGO_INSTALL_SETUP": "1",
    }
    env.update(extra)
    return env


def without(env: dict[str, str], name: str) -> dict[str, str]:
    """Copy of `env` without `name`."""
    return {k: v for k, v in env.items() if k != name}
