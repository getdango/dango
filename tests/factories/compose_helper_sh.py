"""tests/factories/compose_helper_sh.py

Harness that runs the compose helper's sh text against a temp tree with fake tools.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from dango.platform.cloud.compose_helper import (
    ROOT_LOCK_WAIT,
    SUDO_LOCK_WAIT,
    VERB_BUDGET,
    helper_script,
)

IMAGE = "python:3.11-slim"
PROD_PATH_LINE = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; export PATH"
PROD_ROOT_LINE = "S=/srv/dango; D=$S/compose; L=/run/dango-compose"
PROD_WAIT_LINE = f"SUDO_WAIT={SUDO_LOCK_WAIT}; ROOT_WAIT={ROOT_LOCK_WAIT}"

_FAKE_TOOL = """#!/bin/sh
d=$(dirname "$0")
n=$(basename "$0")
l="$(cat "$d/logdir")/$n.$$"
printf '%s\\0' "$@" > "$l.argv"
env -0 > "$l.env"
pwd -P > "$l.cwd"
cat > "$l.stdin"
[ ! -f "$d/$n.sleep" ] || sleep "$(cat "$d/$n.sleep")"
[ ! -f "$d/$n.out" ] || cat "$d/$n.out"
exit "$(cat "$d/$n.rc" 2>/dev/null || echo 0)"
"""

_FAKE_ID = """#!/bin/sh
case "$1" in -u) echo 0;; *) exec /usr/bin/id "$@";; esac
"""

# The test host's files belong to one user, so ``-user root`` is read as "the current user".
_FAKE_FIND = """#!/bin/sh
me=$(/usr/bin/id -un); n=$#; prev=
while [ "$n" -gt 0 ]; do a=$1; shift; n=$((n-1)); o=$a
  [ "$prev" != -user ] || a=$me
  set -- "$@" "$a"; prev=$o; done
exec /usr/bin/find "$@"
"""


def host_is_gnu_linux() -> bool:
    """True when the helper can run directly (GNU coreutils/util-linux/findutils)."""
    if not sys.platform.startswith("linux"):
        return False
    if not all(shutil.which(t) for t in ("sh", "timeout", "flock", "find", "id")):
        return False
    out = subprocess.run(["find", "--version"], capture_output=True, text=True, check=False)
    return out.returncode == 0 and "GNU" in out.stdout


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


@dataclass
class Runner:
    """Runs commands on the host (GNU Linux) or inside a throwaway Docker container."""

    container: str | None = None

    def argv(
        self, argv: list[str], env: dict[str, str] | None
    ) -> tuple[list[str], dict[str, str] | None]:
        if self.container is None:
            return argv, {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **(env or {})}
        flags = [f for k, v in (env or {}).items() for f in ("-e", f"{k}={v}")]
        return ["docker", "exec", "-i", *flags, self.container, *argv], None

    def run(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        full, full_env = self.argv(argv, env)
        return subprocess.run(
            full,
            env=full_env,
            input=stdin or "",
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def popen(self, argv: list[str]) -> subprocess.Popen[str]:
        full, full_env = self.argv(argv, None)
        return subprocess.Popen(
            full,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
        )


@contextmanager
def runner_session(basetemp: Path, user: str | None = None) -> Iterator[Runner]:
    """Yield a Runner: the host when it is GNU Linux (and *user* is None), else a container.

    Skips when neither is available.  The container is removed afterwards.
    """
    if user is None and host_is_gnu_linux():
        yield Runner()
        return
    if not docker_available():
        pytest.skip("needs GNU Linux tools or a running Docker daemon")
    name = f"dango-sh-{uuid.uuid4().hex[:10]}"
    who = user if user is not None else f"{os.getuid()}:{os.getgid()}"
    args = ["docker", "run", "-d", "--rm", "--name", name, "--user", who]
    if user is None:
        args += ["-v", f"{basetemp}:{basetemp}"]
    subprocess.run([*args, IMAGE, "sleep", "600"], check=True, capture_output=True)
    try:
        yield Runner(name)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def patched_script(
    root: Path, sudo_wait: int, root_wait: int, budgets: dict[str, int] | None = None
) -> str:
    """The production helper text with only the path, wait and (optional) budget lines changed."""
    text = helper_script()
    shim = root / "shim"
    swaps = [
        (PROD_PATH_LINE, PROD_PATH_LINE.replace("PATH=", f"PATH={shim}:", 1)),
        (PROD_ROOT_LINE, f"S={root / 'srv'}; D=$S/compose; L={root / 'run' / 'dango-compose'}"),
        (PROD_WAIT_LINE, f"SUDO_WAIT={sudo_wait}; ROOT_WAIT={root_wait}"),
    ]
    for verb, secs in (budgets or {}).items():
        swaps.append((f"  {verb}) echo {VERB_BUDGET[verb]};;", f"  {verb}) echo {secs};;"))
    for old, new in swaps:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


@dataclass
class Tree:
    """A temp server tree plus the patched helper and fake tools."""

    root: Path
    runner: Runner
    script: Path

    @property
    def srv(self) -> Path:
        return self.root / "srv"

    @property
    def compose(self) -> Path:
        return self.srv / "compose"

    @property
    def lock_dir(self) -> Path:
        return self.root / "run" / "dango-compose"

    def call(
        self,
        *args: str,
        sudo: bool = False,
        env: dict[str, str] | None = None,
        timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        """Run the helper; *sudo* sets ``SUDO_UID`` as sudo would for the service user."""
        full_env = dict(env or {})
        if sudo:
            full_env["SUDO_UID"] = "1000"
        return self.runner.run(
            ["sh", str(self.script), *args], full_env, stdin="x\n", timeout=timeout
        )

    def logs(self, tool: str, suffix: str) -> list[Path]:
        return sorted((self.root / "log").glob(f"{tool}.*.{suffix}"))

    def calls(self, tool: str = "docker") -> list[list[str]]:
        out = []
        for path in self.logs(tool, "argv"):
            raw = path.read_bytes().decode()
            out.append(raw.split("\0")[:-1] if raw else [])
        return out

    def env_of_call(self, tool: str = "docker") -> dict[str, str]:
        raw = self.logs(tool, "env")[-1].read_bytes().decode()
        return dict(item.split("=", 1) for item in raw.split("\0") if "=" in item)

    def set_tool(
        self,
        tool: str,
        *,
        sleep: float | None = None,
        rc: int | None = None,
        out: str | None = None,
    ) -> None:
        base = self.root / "shim" if tool == "docker" else self.srv / "venv" / "bin"
        for suffix, value in (("sleep", sleep), ("rc", rc), ("out", out)):
            if value is not None:
                (base / f"{tool}.{suffix}").write_text(f"{value}")


def make_tree(
    tmp: Path,
    runner: Runner,
    *,
    sudo_wait: int = 1,
    root_wait: int = 2,
    budgets: dict[str, int] | None = None,
    pinned: bool = True,
) -> Tree:
    """Build the layout the helper expects (all directories 0755, files 0644)."""
    for rel in ("srv/compose/build", "srv/metabase", "srv/venv/bin", "shim", "log", "run"):
        (tmp / rel).mkdir(parents=True, exist_ok=True)
    for d in (
        tmp,
        tmp / "srv",
        tmp / "srv/compose",
        tmp / "srv/compose/build",
        tmp / "srv/metabase",
        tmp / "srv/venv",
        tmp / "run",
    ):
        d.chmod(0o755)
    if pinned:
        (tmp / "srv/compose/project").write_text("dango-abcd1234\n")
        (tmp / "srv/compose/project-id").write_text("abcd1234-0000-4000-8000-000000000000\n")
        (tmp / "srv/compose/docker-compose.yml").write_text("services: {}\n")
        for f in ("project", "project-id", "docker-compose.yml"):
            (tmp / "srv/compose" / f).chmod(0o644)
    for name, text in (("docker", _FAKE_TOOL), ("id", _FAKE_ID), ("find", _FAKE_FIND)):
        (tmp / "shim" / name).write_text(text)
        (tmp / "shim" / name).chmod(0o755)
    (tmp / "srv/venv/bin/python").write_text(_FAKE_TOOL)
    (tmp / "srv/venv/bin/python").chmod(0o755)
    for d in (tmp / "shim", tmp / "srv/venv/bin"):
        (d / "logdir").write_text(str(tmp / "log"))
    script = tmp / "dango-compose.sh"
    script.write_text(patched_script(tmp, sudo_wait, root_wait, budgets))
    return Tree(tmp, runner, script)
