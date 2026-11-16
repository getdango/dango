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
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "first_user_journey.py"
FAKE_PROJECT = "dango-abcdef12"

# A stand-in for the `dango` executable. FAKE_MODE (comma separated) injects failures.
FAKE_DANGO = r"""
import csv, http.server, os, pathlib, signal, subprocess, sys, time

args = sys.argv[1:]
modes = os.environ.get("FAKE_MODE", "").split(",")
root = pathlib.Path.cwd()
state = pathlib.Path(os.environ["FAKE_STATE_DIR"])
cmd = args[0] if args else ""

SERVER = (
    "import http.server, sys, time\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        self.send_response(401); self.end_headers()\n"
    "    def log_message(self, *a): pass\n"
    "s = http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H)\n"
    "s.timeout = 1\n"
    "end = time.time() + 120\n"
    "while time.time() < end: s.handle_request()\n"
)


def fail(mode, message="boom"):
    if mode in modes:
        text = "Error: %s \u00e9 %s\n" % (message, mode)
        if "cp1252-stderr" in modes:
            sys.stderr.flush()
            sys.stderr.buffer.write(text.encode("cp1252"))
            sys.stderr.buffer.flush()
        else:
            sys.stderr.write(text)
        sys.exit(1)


def record(name, pid):
    with open(root / name, "a", encoding="utf-8") as fh:
        fh.write("%d\n" % pid)


def call(name):
    with open(state / "docker-calls.txt", "a", encoding="utf-8") as fh:
        fh.write(name + "\n")


def platform_port():
    import yaml

    with open(root / ".dango" / "project.yml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["platform"]["port"]


if cmd == "init":
    (root / ".dango").mkdir(exist_ok=True)
    (root / ".dango" / "project.yml").write_text(
        "project:\n  name: fake\n  id: abcdef1234567890abcdef\n"
        "platform:\n  port: 8800\n  metabase_port: 3000\n  dbt_docs_port: 8081\n"
        "  marimo_port: 7805\n",
        encoding="utf-8",
    )
    if "init-no-auth" not in modes:
        (root / ".dango" / "auth.yml").write_text("enabled: true\n", encoding="utf-8")
    keys = [
        "DO_NOT_TRACK", "PYTHONUNBUFFERED", "DANGO_ADMIN_PASSWORD", "PYTHON_KEYRING_BACKEND",
        "PYTHONUTF8", "PYTHONIOENCODING", "NO_COLOR", "TERM",
    ]
    lines = ["%s=%s" % (k, os.environ.get(k, "<unset>")) for k in keys]
    (root / "env-seen.txt").write_text("\n".join(lines), encoding="utf-8")
    fail("fail-init")
    if "init-traceback" in modes:
        print("Traceback (most recent call last):")
    if "init-warnings" in modes:
        print("Initialization Completed with Warnings")
    print("fake init ok")
elif cmd == "validate":
    ok = (root / ".dango" / "sources.yml").is_file() and "fail-validate" not in modes
    print("validate ok" if ok else "validate: failed")
    sys.exit(0 if ok else 1)
elif cmd == "source":
    if "source-no-people" in modes:
        print("No sources configured yet")
    else:
        print((root / ".dango" / "sources.yml").read_text(encoding="utf-8"))
elif cmd == "sync":
    fail("fail-sync")
    if "slow-sync" in modes:
        time.sleep(30)
    marker = root / ".fake-sync-calls"
    calls = int(marker.read_text()) if marker.exists() else 0
    marker.write_text(str(calls + 1))
    with open(root / "data" / "uploads" / "people.csv", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if "grow-on-second-sync" in modes and calls >= 1:
        rows.append({"id": "99", "name": "Extra", "city": "Nowhere"})
    if "short-first-sync" in modes:
        rows = rows[:4]
    import duckdb

    con = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
    if "other-table-second-sync" in modes and calls >= 1:
        con.execute("drop table raw_people.people")
        con.execute("create schema if not exists raw_other")
        con.execute("create table raw_other.people_x (id varchar, name varchar, city varchar)")
        for row in rows:
            con.execute(
                "insert into raw_other.people_x values (?, ?, ?)",
                [row["id"], row["name"], row["city"]],
            )
        rows = []
    elif "no-table" in modes:
        con.execute("create table unrelated (x integer)")
        rows = []
    else:
        con.execute("create schema if not exists raw_people")
        con.execute(
            "create or replace table raw_people.people (id varchar, name varchar, city varchar)"
        )
        for row in rows:
            name = row["name"]
            if "mangled-name" in modes:
                name = name.encode("utf-8").decode("latin-1")  # Zo\u00eb -> mojibake
            con.execute(
                "insert into raw_people.people values (?, ?, ?)", [row["id"], name, row["city"]]
            )
    con.close()
    print("synced", ",".join(r["name"] for r in rows))
elif cmd == "run":
    fail("fail-dbt")
    print("Nothing to do")
elif cmd == "web":
    port = int(args[args.index("--port") + 1])
    record("web-pids.txt", os.getpid())
    print("fake web starting", flush=True)
    if "web-exit-early" in modes:
        print("web: cannot bind, giving up", file=sys.stderr)
        sys.exit(3)
    if "web-ignore-term" in modes:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if "web-orphan-listener" in modes:
        # The listener is a separate process that survives this one being terminated.
        child = subprocess.Popen(
            [sys.executable, "-c", SERVER, str(port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
        record("orphan-pids.txt", child.pid)
        time.sleep(300)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.end_headers()

        def handle_one_request(self):
            if "web-garbage" in modes:
                self.wfile.write(b"this is not http\r\n\r\n")
                self.wfile.flush()
                self.close_connection = True
                return
            super().handle_one_request()

        def log_message(self, *a):
            pass

    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
elif cmd == "start":
    call("start")
    fail("fail-start")
    if "start-no-containers" not in modes:
        with open(state / "containers.txt", "a", encoding="utf-8") as fh:
            fh.write("abc123def456\t%s\n" % root)
    server = subprocess.Popen(
        [sys.executable, "-c", SERVER, str(platform_port())],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=(os.name != "nt"),
    )
    record("platform-pids.txt", server.pid)
    if "start-creates-volume" in modes:
        with open(state / "volumes.txt", "a", encoding="utf-8") as fh:
            fh.write("run-volume\n")
    print("fake platform started")
elif cmd == "status":
    call("status")
    fail("fail-status")
    print("fake status ok")
elif cmd == "stop":
    call("stop")
    pids = (root / "platform-pids.txt").read_text().split() if (root / "platform-pids.txt").exists() else []
    for pid in ([] if "stop-leaves-listener" in modes else pids):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
    if "leftover" not in modes:
        keep = [ln for ln in (state / "containers.txt").read_text().splitlines() if not ln.endswith("\t%s" % root)] if (state / "containers.txt").exists() else []
        (state / "containers.txt").write_text("".join(ln + "\n" for ln in keep), encoding="utf-8")
    print("fake platform stopped")
else:
    print("unknown command", cmd, file=sys.stderr)
    sys.exit(2)
"""

# A stand-in for `docker`: containers/volumes live in files under FAKE_STATE_DIR.
FAKE_DOCKER = r"""
import os, pathlib, sys

args = sys.argv[1:]
modes = os.environ.get("FAKE_MODE", "").split(",")
state = pathlib.Path(os.environ["FAKE_STATE_DIR"])


def project_filter():
    for a in args:
        if a.startswith("label=com.docker.compose.project="):
            return a.split("=", 2)[2]
    return ""


def lines(name):
    path = state / name
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


if args[:1] == ["ps"]:
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
elif args[:3] == ["volume", "rm", "-f"]:
    left = [v for v in lines("volumes.txt") if v != args[3]]
    (state / "volumes.txt").write_text("".join(v + "\n" for v in left), encoding="utf-8")
    with open(state / "docker-rm.txt", "a", encoding="utf-8") as fh:
        fh.write("volume " + args[3] + "\n")
else:
    print("fake docker: unsupported", args, file=sys.stderr)
    sys.exit(2)
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
) -> subprocess.CompletedProcess[str]:
    """Run the journey script against a fake dango (and, with docker=True, a fake docker)."""
    exe = dango or make_fake_exe(tmp_path / "fakebin", "dango", FAKE_DANGO)
    drop = {"PYTHON_KEYRING_BACKEND", "PYTHONUTF8", "PYTHONIOENCODING"}
    full_env = {k: v for k, v in os.environ.items() if k not in drop}
    full_env["FAKE_MODE"] = mode
    full_env["FAKE_PROJECT"] = FAKE_PROJECT
    (tmp_path / "fakestate").mkdir(exist_ok=True)
    full_env["FAKE_STATE_DIR"] = str(tmp_path / "fakestate")
    if docker:
        bin_dir = tmp_path / "dockerbin"
        make_fake_exe(bin_dir, "docker", FAKE_DOCKER)
        full_env["PATH"] = str(bin_dir) + os.pathsep + full_env.get("PATH", "")
    full_env.update(env or {})
    argv = [
        sys.executable,
        str(SCRIPT),
        "--dango",
        str(exe),
        "--python",
        sys.executable,
        "--workdir",
        str(tmp_path / "work"),
        "--log",
        str(tmp_path / log_name),
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
