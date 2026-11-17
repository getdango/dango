"""tests/factories/journey_fake_dango.py

Source of the fake `dango` executable used by the first-user journey tests; failures and hangs
are injected per command through FAKE_MODE, FAKE_FAIL and FAKE_HANG.
"""

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


def bump(key):
    path = state / ("count-%s.txt" % key)
    n = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(n))
    return n


def injected(var, key, n):
    name, _, nth = os.environ.get(var, "").partition(":")
    return name == key and int(nth or 1) == n


key = "source-list" if args[:2] == ["source", "list"] else cmd
nth = bump(key)
if key != "stop":  # stop does its work first, then fails
    if injected("FAKE_HANG", key, nth):
        time.sleep(60)
    if injected("FAKE_FAIL", key, nth):
        sys.stderr.write("Error: injected failure of %s\n" % key)
        sys.exit(1)

EXPECTED_ARGS = {
    "init": ["init", "--skip-wizard"],
    "validate": ["validate"],
    "source-list": ["source", "list"],
    "sync": ["sync", "--yes"],
    "run": ["run"],
    "start": ["start", "--yes"],
    "status": ["status"],
    "stop": ["stop"],
}
if key in EXPECTED_ARGS and args != EXPECTED_ARGS[key]:
    sys.stderr.write("fake dango: unexpected arguments %r\n" % (args,))
    sys.exit(2)
if cmd == "web" and not (
    len(args) == 5 and args[:4] == ["web", "--host", "127.0.0.1", "--port"] and args[4].isdigit()
):
    sys.stderr.write("fake dango: unexpected web arguments %r\n" % (args,))
    sys.exit(2)

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
        "PYTHONUTF8", "PYTHONIOENCODING", "NO_COLOR", "TERM", "COLUMNS",
    ]
    lines = ["%s=%s" % (k, os.environ.get(k, "<unset>")) for k in keys]
    (root / "env-seen.txt").write_text("\n".join(lines), encoding="utf-8")
    fail("fail-init")
    if "init-traceback" in modes:
        print("Traceback (most recent call last):")
    if "init-warnings" in modes:
        print("Initialization Completed with Warnings")
    print("fake init ok")
elif cmd == "validate" and "validate-rows" in modes:
    # Shaped like the real table: the flagged rows come early, then more than 15 passing lines.
    print("Validating Dango Project")
    print("\nPackage")
    print(" Check       Status    Details")
    print(" DuckDB      \u2713 PASS")
    print("\nCLI")
    print(" dbt         \u2717 FAIL  Error checking dbt: Command '['dbt', '--version']' timed out after 5 seconds")
    print(" git         \u26a0 WARN  git not found")
    print("\nPermissions")
    for i in range(24):
        print(" dir%d        \u2713 PASS" % i)
    print("Validation Summary")
    print("Passed: 25  Warnings: 1  Failed: 1")
    sys.exit(1)
elif cmd == "validate":
    ok = (root / ".dango" / "sources.yml").is_file() and "fail-validate" not in modes
    print("validate ok" if ok else "validate: failed")
    sys.exit(0 if ok else 1)
elif cmd == "source":
    if args != ["source", "list"]:
        sys.stderr.write("fake dango: unknown source subcommand %r\n" % (args[1:],))
        sys.exit(2)
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
    (state / "start-port.txt").write_text(str(platform_port()), encoding="utf-8")
    if "start-no-listener" not in modes:
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
    if injected("FAKE_FAIL", "stop", nth):
        sys.exit(1)
else:
    print("unknown command", cmd, file=sys.stderr)
    sys.exit(2)
"""
