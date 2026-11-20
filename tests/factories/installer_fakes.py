"""tests/factories/installer_fakes.py

Runs install.sh against fake python3 and dango executables on an isolated PATH, recording every
python invocation, so tests can assert what the installer would have run. POSIX-only.
"""

from __future__ import annotations

import json
import os
import pty
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install.sh"
BASH = "/bin/bash"  # the installer's shebang interpreter (bash 3.2 on macOS)
TOOLS = (
    "cut",
    "tr",
    "grep",
    "sed",
    "uname",
    "mkdir",
    "basename",
    "cat",
    "sort",
    "env",
    "tail",
    "rm",
    "ln",
)

_FAKE_PYTHON = """#!{python}
import json, os, pathlib, sys

args = sys.argv[1:]
env = os.environ
with open(env["FAKE_RECORD"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")
code = args[1] if args[:1] == ["-c"] and len(args) > 1 else ""
if args[:1] == ["-c"]:
    if "base_prefix" in code:
        print(env.get("FAKE_IN_VENV", "0"))
    elif "EXTERNALLY-MANAGED" in code:
        print(env.get("FAKE_EXTERNALLY_MANAGED", "0"))
    elif "importlib.metadata" in code:
        have = env.get("FAKE_INSTALLED") or ("somepkg=1.0" if env.get("FAKE_WOULD_UPGRADE") else "")
        installed = dict(p.split("=") for p in have.split(";") if p)
        for name in args[2:]:
            if name in installed:
                print(name, installed[name])
    else:
        print(env.get("FAKE_PY_VERSION", "3.12"))
elif args[:2] == ["-m", "venv"]:
    target = pathlib.Path(args[2])
    if env.get("FAKE_VENV_FAIL"):
        target.mkdir(parents=True, exist_ok=True)
        print("The virtual environment was not created successfully because ensurepip is not "
              "available.", file=sys.stderr)
        sys.exit(1)
    (target / "bin").mkdir(parents=True, exist_ok=True)
    (target / "bin" / "activate").write_text("", encoding="utf-8")
elif args[:3] == ["-m", "pip", "install"] and env.get("FAKE_PIP_FAIL"):
    # FAKE_PIP_FAIL=pip fails the pip upgrade; FAKE_PIP_FAIL=dango fails the --upgrade of Dango
    is_pip_upgrade = args[3:5] == ["--upgrade", "pip"]
    if (env["FAKE_PIP_FAIL"] == "pip") == is_pip_upgrade and "--upgrade" in args:
        print("WARNING: Retrying (Retry(total=4)) after connection broken", file=sys.stderr)
        sys.exit(1)
elif args[:3] == ["-m", "pip", "--version"]:
    if env.get("FAKE_NO_PIP"):
        print("/usr/bin/python3: No module named pip", file=sys.stderr)
        sys.exit(1)
    print("pip 24.0")
elif args[:2] == ["-m", "site"]:
    print(env["FAKE_USER_BASE"])
elif "--dry-run" in args:
    if env.get("FAKE_PIP_NO_DRYRUN"):
        # pip older than 22.2 has no --dry-run
        print("Usage:\\n  pip install [options] <requirement specifier> ...", file=sys.stderr)
        print("no such option: --dry-run", file=sys.stderr)
        sys.exit(2)
    if env.get("FAKE_DRY_RUN_OUTPUT"):
        print(env["FAKE_DRY_RUN_OUTPUT"])
    elif env.get("FAKE_WOULD_UPGRADE"):
        # pip never prints "Would upgrade": an upgrade is "Would install <new version>"
        print("Would install somepkg-2.0 getdango-1.0.15")
sys.exit(0)
"""

_FAKE_DANGO = """#!{python}
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_RECORD"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(["dango", *args]) + "\\n")
if args[:1] == ["--version"]:
    print("dango, version 1.0.15")
if args[:1] == ["init"] and os.environ.get("FAKE_DANGO_INIT_FAIL"):
    print("Error: could not write .dango/project.yml", file=sys.stderr)
    sys.exit(3)
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


def _run_on_terminal(
    argv: list[str], cwd: Path, env: dict[str, str], typed: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run `argv` with a real controlling terminal; `typed` is what the person types."""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execve(argv[0], argv, env)
    os.write(fd, typed.encode("utf-8"))
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                os.kill(pid, signal.SIGKILL)
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                data = os.read(fd, 4096)
            except OSError:  # the child closed the terminal
                break
            if not data:
                break
            chunks.append(data)
    finally:
        os.close(fd)
        _, status = os.waitpid(pid, 0)
    out = b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")
    return subprocess.CompletedProcess(argv, os.waitstatus_to_exitcode(status), out, "")


def run_installer(
    base: Path,
    env: dict[str, str],
    *,
    dango_on_path: bool = True,
    seed_project: str = "",
    snippet: str = "",
    existing_dirs: tuple[str, ...] = (),
    typed: str | None = None,
    timeout: float = 60,
) -> InstallRun:
    """Run install.sh (or, with `snippet`, a bash snippet after sourcing it) with no terminal.

    seed_project: "" (new project), "venv" (existing project with a venv) or "novenv".
    typed: when given, run on a real terminal and type these lines (interactive mode).
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
    if typed is not None:
        proc = _run_on_terminal(argv, cwd, full_env, typed, timeout)
        return InstallRun(proc, record, cwd, home, user_base / "bin")
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
