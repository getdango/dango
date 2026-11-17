"""tests/factories/journey_fakes.py

Fake `dango` and `docker` executables plus a runner for scripts/first_user_journey.py tests.
POSIX-only helpers (they signal processes with os.kill); the journey tests are not in the
Windows unit subset.
"""

import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from tests.factories.journey_fake_dango import FAKE_DANGO

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "first_user_journey.py"
FAKE_PROJECT = "dango-abcdef12"

# A stand-in for `docker`: containers/volumes live in files under FAKE_STATE_DIR.
FAKE_DOCKER = r"""
import os, pathlib, sys

args = sys.argv[1:]
modes = os.environ.get("FAKE_MODE", "").split(",")
state = pathlib.Path(os.environ["FAKE_STATE_DIR"])
key = "d-ps" if args[:1] == ["ps"] else "d-volume-ls" if args[:2] == ["volume", "ls"] else (
    "d-rm" if args[:2] == ["rm", "-f"] else "d-volume-rm" if args[:3] == ["volume", "rm", "-f"] else "d-other"
)
count_file = state / ("count-%s.txt" % key)
nth = int(count_file.read_text()) + 1 if count_file.exists() else 1
count_file.write_text(str(nth))
name, _, which = os.environ.get("FAKE_FAIL", "").partition(":")
if name == key and int(which or 1) == nth:
    print("injected failure of %s" % key, file=sys.stderr)
    sys.exit(1)


def project_filter():
    for a in args:
        if a.startswith("label=com.docker.compose.project="):
            return a.split("=", 2)[2]
    return ""


def lines(name):
    path = state / name
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


if args[:1] == ["ps"]:
    if "-a" not in args:  # the journey must list stopped containers too
        print("fake docker: ps must be called with -a", file=sys.stderr)
        sys.exit(2)
    if "ps-fails" in modes:
        print("Cannot connect to the Docker daemon", file=sys.stderr)
        sys.exit(1)
    if project_filter() == os.environ["FAKE_PROJECT"]:
        for line in lines("containers.txt"):
            print(line if "--format" in args else line.split("\t")[0])
elif args[:2] == ["volume", "ls"]:
    if "volume-ls-fails" in modes:
        print("volume list failed", file=sys.stderr)
        sys.exit(1)
    if project_filter() == os.environ["FAKE_PROJECT"]:
        print("\n".join(lines("volumes.txt")))
elif args[:2] == ["rm", "-f"]:
    left = [c for c in lines("containers.txt") if c.split("\t")[0] != args[2]]
    (state / "containers.txt").write_text("".join(c + "\n" for c in left), encoding="utf-8")
    with open(state / "docker-rm.txt", "a", encoding="utf-8") as fh:
        fh.write(args[2] + "\n")
    if "docker-bad-bytes" in modes:
        sys.stderr.buffer.write(bytes([0x81]) + b" daemon message\n")
        sys.stderr.buffer.flush()
elif args[:3] == ["volume", "rm", "-f"]:
    left = [v for v in lines("volumes.txt") if v != args[3]]
    (state / "volumes.txt").write_text("".join(v + "\n" for v in left), encoding="utf-8")
    with open(state / "docker-rm.txt", "a", encoding="utf-8") as fh:
        fh.write("volume " + args[3] + "\n")
else:
    print("fake docker: unsupported", args, file=sys.stderr)
    sys.exit(2)
"""


# A stand-in for the venv python: runs the real interpreter, but can fail or hang one of the
# journey's helper snippets (set-ports, warehouse count, project info) on the nth call.
FAKE_PYTHON = r"""
import os, pathlib, sys, time

state = pathlib.Path(os.environ["FAKE_STATE_DIR"])
code = sys.argv[sys.argv.index("-c") + 1] if "-c" in sys.argv else ""
key = (
    "py-set-ports" if "yaml.safe_dump" in code
    else "py-count" if "NO_TABLE" in code
    else "py-project-info" if "get_compose_project_name" in code
    else ""
)
if key:
    path = state / ("count-%s.txt" % key)
    nth = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(nth))
    for var in ("FAKE_HANG", "FAKE_FAIL"):
        name, _, which = os.environ.get(var, "").partition(":")
        if name == key and int(which or 1) == nth:
            if var == "FAKE_HANG":
                time.sleep(60)
            print("injected failure of %s" % key, file=sys.stderr)
            sys.exit(1)
os.execv(os.environ["FAKE_REAL_PYTHON"], [os.environ["FAKE_REAL_PYTHON"]] + sys.argv[1:])
"""


def make_fake_exe(directory: Path, name: str, body: str) -> Path:
    """Create an executable `name` in `directory` that runs `body` with this interpreter."""
    directory.mkdir(parents=True, exist_ok=True)
    impl = directory / f"{name}_impl.py"
    impl.write_text(body, encoding="utf-8")
    if os.name == "nt":
        exe = directory / f"{name}.cmd"
        exe.write_text(f'@"{sys.executable}" "{impl}" %*\r\n', encoding="utf-8")
    else:
        exe = directory / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n', encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def run_journey(
    tmp_path: Path,
    mode: str = "",
    extra: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
    dango: Path | None = None,
    log_name: str = "journey.log",
    docker: bool = False,
    steps: str | None = None,
    relative: bool = False,
    seed: dict[str, str] | None = None,
    bare_dango: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the journey script against a fake dango (and, with docker=True, a fake docker)."""
    exe = dango or make_fake_exe(tmp_path / "fakebin", "dango", FAKE_DANGO)
    fake_python = make_fake_exe(tmp_path / "fakebin", "python", FAKE_PYTHON)
    drop = {"PYTHON_KEYRING_BACKEND", "PYTHONUTF8", "PYTHONIOENCODING"}
    full_env = {k: v for k, v in os.environ.items() if k not in drop}
    full_env["FAKE_MODE"] = mode
    full_env["FAKE_PROJECT"] = FAKE_PROJECT
    full_env["FAKE_REAL_PYTHON"] = sys.executable
    (tmp_path / "fakestate").mkdir(exist_ok=True)
    for name, content in (seed or {}).items():  # pre-existing fake docker state
        (tmp_path / "fakestate" / name).write_text(content, encoding="utf-8")
    full_env["FAKE_STATE_DIR"] = str(tmp_path / "fakestate")
    if docker:
        bin_dir = tmp_path / "dockerbin"
        make_fake_exe(bin_dir, "docker", FAKE_DOCKER)
        full_env["PATH"] = str(bin_dir) + os.pathsep + full_env.get("PATH", "")
    full_env.update(env or {})

    def shown(path: Path) -> str:
        return os.path.relpath(path, tmp_path) if relative else str(path)

    if bare_dango:
        # `--dango dango` is found on PATH next to the real venv python; an earlier PATH entry
        # holds a decoy `python` that must not be used.
        decoy = tmp_path / "decoybin"
        make_fake_exe(decoy, "python", "import sys\nsys.exit(99)\n")
        full_env["PATH"] = os.pathsep.join([str(decoy), str(exe.parent), full_env.get("PATH", "")])
    argv = [
        sys.executable,
        str(SCRIPT),
        "--dango",
        "dango" if bare_dango else shown(exe),
        *([] if bare_dango else ["--python", shown(fake_python)]),
        "--workdir",
        shown(tmp_path / "work"),
        "--log",
        shown(tmp_path / log_name),
        "--stop-grace",
        "1",
        *(["--with-docker"] if docker else []),
        *(["--steps", steps] if steps else []),
        *extra,
    ]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=full_env,
        cwd=tmp_path if relative else None,
    )


def pid_alive(pid: int) -> bool:
    """True when a process with this pid exists."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def tracked_pids(tmp_path: Path) -> list[int]:
    """Pids the fakes recorded (web, orphan listener, platform) in the journey workdir."""
    pids: list[int] = []
    for path in (tmp_path / "work").glob("*-pids.txt"):
        pids.extend(int(p) for p in path.read_text(encoding="utf-8").split())
    return pids


def reap_orphans(tmp_path: Path) -> list[int]:
    """Kill every tracked fake process that is still alive; return the pids that were."""
    alive = [p for p in tracked_pids(tmp_path) if pid_alive(p)]
    for pid in alive:
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass
    deadline = time.monotonic() + 5
    while any(pid_alive(p) for p in alive) and time.monotonic() < deadline:
        time.sleep(0.05)
    return alive


def lines_of(result: subprocess.CompletedProcess[str], verdict: str) -> list[str]:
    """Report lines of one verdict (PASS, FAIL or SKIP) from the journey's stdout."""
    return [ln for ln in result.stdout.splitlines() if ln.startswith(verdict + " ")]


Outcome = tuple["subprocess.CompletedProcess[str]", Path]


def run_scenarios(
    tmp_path_factory: Any,
    scenarios: dict[str, dict[str, Any]],
    workers: int = 6,
    expect_survivors: frozenset[str] = frozenset(),
) -> Iterator[dict[str, Outcome]]:
    """Run every scenario once, in parallel, each in its own directory.

    Yields {name: (result, directory)}; afterwards kills every fake process still alive and fails
    if one survived in a scenario that is not named in `expect_survivors`.
    """
    dirs = {name: tmp_path_factory.mktemp(name) for name in scenarios}

    def run(name: str) -> subprocess.CompletedProcess[str]:
        return run_journey(dirs[name], **scenarios[name])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, scenarios))
    yield {name: (res, dirs[name]) for name, res in zip(scenarios, results, strict=True)}
    survivors = [
        pid for name, d in dirs.items() for pid in reap_orphans(d) if name not in expect_survivors
    ]
    assert not survivors, f"fake processes survived: {survivors}"
