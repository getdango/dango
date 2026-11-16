"""tests/unit/test_first_user_journey.py

scripts/first_user_journey.py walks a first-user session (init, source, sync, count, dbt, web
start/stop/restart, second sync, optional docker) and reports PASS/FAIL/SKIP per step.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "first_user_journey.py"

# A stand-in for the `dango` executable. FAKE_MODE (comma separated) injects failures.
FAKE_DANGO = r"""
import csv, http.server, os, pathlib, signal, sys, time

args = sys.argv[1:]
modes = os.environ.get("FAKE_MODE", "").split(",")
root = pathlib.Path.cwd()
cmd = args[0] if args else ""


def fail(mode, message="boom"):
    if mode in modes:
        sys.stderr.write("Error: %s é %s\n" % (message, mode))
        sys.exit(1)


if cmd == "init":
    (root / ".dango").mkdir(exist_ok=True)
    (root / ".dango" / "project.yml").write_text(
        "platform:\n  port: 8800\n  metabase_port: 3000\n  dbt_docs_port: 8081\n"
        "  marimo_port: 7805\n",
        encoding="utf-8",
    )
    (root / ".dango" / "auth.yml").write_text("enabled: true\n", encoding="utf-8")
    keys = ["DO_NOT_TRACK", "PYTHONUNBUFFERED", "DANGO_ADMIN_PASSWORD", "PYTHON_KEYRING_BACKEND"]
    lines = ["%s=%s" % (k, os.environ.get(k, "<unset>")) for k in keys]
    (root / "env-seen.txt").write_text("\n".join(lines), encoding="utf-8")
    fail("fail-init")
    print("fake init ok")
elif cmd == "validate":
    ok = (root / ".dango" / "sources.yml").is_file()
    print("validate ok" if ok else "validate: no sources.yml")
    sys.exit(0 if ok else 1)
elif cmd == "source":
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
    import duckdb

    con = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
    con.execute("create schema if not exists raw_people")
    con.execute("create or replace table raw_people.people (id varchar, name varchar, city varchar)")
    for row in rows:
        con.execute("insert into raw_people.people values (?, ?, ?)", [row["id"], row["name"], row["city"]])
    con.close()
    print("synced", ",".join(r["name"] for r in rows))
elif cmd == "run":
    fail("fail-dbt")
    print("Nothing to do")
elif cmd == "web":
    port = int(args[args.index("--port") + 1])
    with open(root / "web-pids.txt", "a", encoding="utf-8") as fh:
        fh.write("%d\n" % os.getpid())
    if "web-exit-early" in modes:
        print("web: cannot bind, giving up", file=sys.stderr)
        sys.exit(3)
    if "web-ignore-term" in modes:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.end_headers()

        def log_message(self, *a):
            pass

    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
else:
    print("unknown command", cmd, file=sys.stderr)
    sys.exit(2)
"""


def _make_fake_dango(tmp_path: Path) -> Path:
    impl = tmp_path / "fake_dango_impl.py"
    impl.write_text(FAKE_DANGO, encoding="utf-8")
    if os.name == "nt":
        exe = tmp_path / "fake_dango.cmd"
        exe.write_text(f'@"{sys.executable}" "{impl}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "fake_dango"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n', encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def _journey(
    tmp_path: Path,
    mode: str = "",
    extra: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
    dango: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    exe = dango or _make_fake_dango(tmp_path)
    full_env = {k: v for k, v in os.environ.items() if k != "PYTHON_KEYRING_BACKEND"}
    full_env["FAKE_MODE"] = mode
    full_env.update(env or {})
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dango",
            str(exe),
            "--python",
            sys.executable,
            "--workdir",
            str(tmp_path / "work"),
            "--log",
            str(tmp_path / "journey.log"),
            "--stop-grace",
            "1",
            *extra,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=full_env,
    )


def _lines(result: subprocess.CompletedProcess[str], verdict: str) -> list[str]:
    return [ln for ln in result.stdout.splitlines() if ln.startswith(verdict + " ")]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_all_steps_pass_and_docker_is_skipped_by_default(tmp_path: Path) -> None:
    result = _journey(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    for step in ("init", "add-source", "sync", "count", "dbt", "web", "second-sync"):
        assert any(ln.startswith(f"PASS {step}") for ln in result.stdout.splitlines()), step
    assert "SKIP docker: not requested" in result.stdout
    assert not _lines(result, "FAIL")
    assert "Traceback" not in result.stderr


def test_ports_are_changed_from_the_defaults_and_distinct(tmp_path: Path) -> None:
    import yaml

    result = _journey(tmp_path)
    assert result.returncode == 0, result.stdout
    data = yaml.safe_load((tmp_path / "work" / ".dango" / "project.yml").read_text("utf-8"))
    ports = [data["platform"][k] for k in ("port", "metabase_port", "dbt_docs_port", "marimo_port")]
    assert not set(ports) & {8800, 3000, 8081, 7805}
    assert len(set(ports)) == 4


def test_csv_is_utf8_with_lf_and_non_ascii_round_trips_into_the_log(tmp_path: Path) -> None:
    result = _journey(tmp_path)
    assert result.returncode == 0, result.stdout
    raw = (tmp_path / "work" / "data" / "uploads" / "people.csv").read_bytes()
    assert "Zoë".encode() in raw
    assert b"\r" not in raw
    assert raw.splitlines()[0] == b"id,name,city"
    assert len(raw.splitlines()) == 6
    log = (tmp_path / "journey.log").read_text(encoding="utf-8")
    assert "Zoë" in log
    assert "dango" in log and "exit" in log.lower()


def test_failed_step_skips_dependents_and_exits_one(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="fail-sync")
    assert result.returncode == 1
    assert "FAIL sync: exit code 1" in result.stdout
    assert "SKIP count: depends on sync" in result.stdout
    assert "SKIP dbt: depends on sync" in result.stdout
    assert "SKIP second-sync: depends on" in result.stdout
    assert any(ln.startswith("PASS web") for ln in result.stdout.splitlines())
    assert "boom" in result.stdout  # tail of the failing step


def test_failed_init_skips_everything_that_needs_a_project(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="fail-init")
    assert result.returncode == 1
    assert "FAIL init" in result.stdout
    assert "SKIP add-source: depends on init" in result.stdout
    assert "SKIP web: depends on init" in result.stdout
    assert not any(ln.startswith("PASS") for ln in result.stdout.splitlines())


def test_missing_dango_executable_fails_init_without_traceback(tmp_path: Path) -> None:
    result = _journey(tmp_path, dango=tmp_path / "nope" / "dango")
    assert result.returncode == 1
    assert "FAIL init" in result.stdout
    assert "SKIP sync" in result.stdout
    assert "Traceback" not in result.stderr


def test_web_step_fails_when_the_process_exits_before_it_is_up(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="web-exit-early")
    assert result.returncode == 1
    assert "FAIL web" in result.stdout
    assert "web: cannot bind, giving up" in result.stdout
    assert any(ln.startswith("PASS second-sync") for ln in result.stdout.splitlines())


def test_web_is_started_twice_and_every_process_and_the_port_are_gone(tmp_path: Path) -> None:
    result = _journey(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "restart" in next(ln for ln in result.stdout.splitlines() if ln.startswith("PASS web"))
    pids = [int(p) for p in (tmp_path / "work" / "web-pids.txt").read_text("utf-8").split()]
    assert len(pids) == 2 and pids[0] != pids[1]
    assert not any(_pid_alive(p) for p in pids)


def test_web_child_that_ignores_terminate_is_killed(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="web-ignore-term")
    assert "Traceback" not in result.stderr
    pids = [int(p) for p in (tmp_path / "work" / "web-pids.txt").read_text("utf-8").split()]
    assert pids
    assert not any(_pid_alive(p) for p in pids)
    assert result.returncode == 0, result.stdout


def test_second_sync_that_changes_the_count_fails_idempotency(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="grow-on-second-sync")
    assert result.returncode == 1
    failing = next(ln for ln in result.stdout.splitlines() if ln.startswith("FAIL second-sync"))
    assert "5" in failing and "6" in failing


def test_step_timeout_is_reported_and_does_not_hang(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="slow-sync", extra=("--step-timeout", "2"))
    assert result.returncode == 1
    assert "FAIL sync: timed out after 2s" in result.stdout
    assert "SKIP count: depends on sync" in result.stdout


def test_child_environment_carries_the_journey_variables(tmp_path: Path) -> None:
    result = _journey(tmp_path)
    assert result.returncode == 0, result.stdout
    seen = (tmp_path / "work" / "env-seen.txt").read_text("utf-8")
    assert "DO_NOT_TRACK=1" in seen
    assert "PYTHONUNBUFFERED=1" in seen
    assert "DANGO_ADMIN_PASSWORD=<unset>" not in seen
    assert "PYTHON_KEYRING_BACKEND=<unset>" in seen


def test_isolate_keyring_flag_sets_the_fail_backend(tmp_path: Path) -> None:
    result = _journey(tmp_path, extra=("--isolate-keyring",))
    assert result.returncode == 0, result.stdout
    seen = (tmp_path / "work" / "env-seen.txt").read_text("utf-8")
    assert "PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring" in seen


def test_non_ascii_output_does_not_crash_the_reporter_under_a_cp1252_stdout(
    tmp_path: Path,
) -> None:
    env = {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    result = _journey(tmp_path, mode="fail-sync", env=env)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "FAIL sync" in result.stdout
    assert "\\xe9" in result.stdout  # the e-acute in the failing step's stderr, escaped
    log = (tmp_path / "journey.log").read_text(encoding="utf-8")
    assert "é" in log
