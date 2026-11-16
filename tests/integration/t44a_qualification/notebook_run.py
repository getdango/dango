"""tests/integration/t44a_qualification/notebook_run.py

One real marimo kernel, exact native wait/commit witness and bounded safe evidence.
"""

import importlib.metadata
import json
import os
import secrets
import signal
import subprocess
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PINS = json.loads((HERE / "notebook-pins.json").read_text())
MIB = 1024**2
EXTRA_PEAK = 768 * MIB
STATUS = "PASS_REAL_MARIMO_CAPTURED_HELD_350_FRESH_360"
FILES = [
    "notebook_run.py",
    "notebook_fixture.py",
    "captured_notebook.py",
    "sitecustomize.py",
    "notebook-pins.json",
    "requirements-notebook.txt",
]


def artifacts(producer):
    """Add the exact released core closure without changing shared source-producer pins."""
    from held_reader import artifact_union

    return artifact_union({"artifacts": producer}, PINS["artifacts"])


def remaining():
    """Charge new archives/three materialized copies and both evidence copies before growth."""
    return EXTRA_PEAK + sum(
        p["compressed"] + 3 * p["expanded_upper"]
        for p in PINS["artifacts"]
        if not p["shared_existing"]
    )


def verify_install():
    """Verify installed released versions, native members and notices before notebook import."""
    from held_reader import digest

    for pin in PINS["artifacts"]:
        dist = importlib.metadata.distribution(pin["name"])
        assert dist.version == pin["version"], "Notebook closure version changed"
        for member in pin["native_members"] + pin["notice_members"]:
            data = dist.locate_file(member["path"]).read_bytes()
            assert len(data) == member["size"] and digest(data) == member["sha256"]


def witness(rows, locks, key, owner):
    """Refuse prepare-only, unrelated, ambiguous or nonexecuting PostgreSQL witnesses."""
    matching = [r for r in rows if f"public.t44a_held_gate({key})" in r[1]]
    assert len(matching) == 1, "Missing/ambiguous executed notebook gate"
    pid, sql, wait_type, wait, state, blockers, started = matching[0]
    assert sql == f'COPY (SELECT public.t44a_held_gate({key}) AS zero) TO STDOUT (FORMAT "binary");'
    assert (wait_type, wait, state) == ("Lock", "advisory", "active")
    assert blockers == "{" + str(owner) + "}" and pid != owner
    assert sorted(locks) == sorted([(owner, True, "ExclusiveLock"), (pid, False, "ExclusiveLock")])
    return {"pid": pid, "copy_sql": sql, "owner_pid": owner, "key": key, "backend_start": started}


def observe(con, key, owner):
    """Observe the actual PG executing COPY and its exact transaction-lock dependency."""
    from notebook_fixture import pg

    rows = pg(
        con,
        "SELECT pid,query,wait_event_type,wait_event,state,pg_blocking_pids(pid)::text,backend_start::text FROM pg_stat_activity WHERE datname='probe' AND state='active' AND wait_event_type='Lock' AND wait_event='advisory'",
    )
    locks = pg(
        con,
        f"SELECT pid,granted,mode FROM pg_locks WHERE locktype='advisory' AND database=(SELECT oid FROM pg_database WHERE datname='probe') AND classid=0 AND objid={key} AND objsubid=1",
    )
    if not any(f"public.t44a_held_gate({key})" in r[1] for r in rows):
        return None
    return witness(rows, locks, key, owner)


def cli(case, binding_hash):
    """Run the released CLI, without sandbox/bootstrap or ambient provider credentials."""
    import sys

    environment = {
        "PATH": "/work/venv/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/probe",
        "LANG": "C.UTF-8",
        "PYTHONPATH": "/scripts",
        "PGPASSFILE": "/secrets/pgpass-python",
        "T44A_PROCESS_AUDIT": "1",
        "T44A_NOTEBOOK_CASE": case,
        "T44A_BINDING_SHA256": binding_hash,
        "T44A_SELECTED_SCHEMA": "t44a_producer_raw" if case == "catalog" else "t44a_producer_actor",
        "AWS_EC2_METADATA_DISABLED": "true",
        "GCE_METADATA_DISABLED": "true",
    }
    assert sys.executable == "/work/venv/bin/python"
    process = subprocess.Popen(
        [
            "/work/venv/bin/marimo",
            "export",
            "html",
            "--no-sandbox",
            "--no-include-code",
            "--no-watch",
            "-o",
            "/work/notebook-" + case + ".html",
            "/scripts/captured_notebook.py",
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    process.t44a_case = case
    return process


def finish(process, report, case, timeout=120):
    """Capture bounded original child output; leaks fail before any public sanitization."""
    from held_reader import digest

    stdout, stderr = process.communicate(timeout=timeout)
    known = json.loads(Path("/secrets/catalog.json").read_text()).values()
    assert len(stdout) + len(stderr) <= 8 * MIB, "Notebook console bound"
    leaked = any(s.encode() in stdout + stderr for s in known)
    safe_stdout, safe_stderr = stdout.decode(errors="replace"), stderr.decode(errors="replace")
    for value in known:
        safe_stdout = safe_stdout.replace(value, "[REDACTED]")
        safe_stderr = safe_stderr.replace(value, "[REDACTED]")
    report["commands"].append(
        {
            "case": case,
            "argv": process.args,
            "exit": process.returncode,
            "stdout": safe_stdout,
            "stderr": safe_stderr,
            "raw_secret_leak": leaked,
            "pid": process.pid,
            "stdout_sha256": digest(stdout),
            "stderr_sha256": digest(stderr),
        }
    )
    process.receipted = True
    assert not leaked, "Notebook raw console secret; sanitized failure retained"
    return process.returncode


def stop_child(process):
    """Cancel only the owned notebook process group, including its kernel, on every failure."""
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


def output_check():
    """Scan complete HTML, cell, process and console evidence; export only safe hashes/JSON."""
    from held_reader import digest

    known = json.loads(Path("/secrets/catalog.json").read_text()).values()
    result = {}
    for path in Path("/work").glob("notebook-*"):
        if path.is_file():
            assert path.stat().st_size <= 32 * MIB, "Notebook original output bound"
            data = path.read_bytes()
            assert not any(v.encode() in data for v in known), "Notebook original output secret"
            result[path.name] = {"bytes": len(data), "sha256": digest(data)}
    assert (
        sum(p.stat().st_size for p in Path("/work").glob("notebook-*") if p.is_file()) <= 64 * MIB
    )
    return result


def fixture_fit():
    """Count added branch/cache/report originals and spill, retaining64MiB export reserve."""
    from held_reader import DATA
    from notebook_fixture import pg, pg_connection

    paths = [DATA, Path("/work/notebook-spill"), Path("/home/probe/.cache")]
    paths += list(Path("/work").glob("notebook-*"))
    unique = {p.resolve(): p for root in paths for p in [root, *root.rglob("*")] if p.exists()}
    assert not any(p.is_symlink() for p in unique.values())
    allocated = sum(p.stat().st_blocks * 512 for p in unique.values())
    with pg_connection() as con:
        allocated += pg(
            con,
            "SELECT coalesce(sum(pg_total_relation_size(c.oid)),0)::bigint FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'",
        )[0][0]
    assert allocated <= EXTRA_PEAK - 64 * MIB, "Notebook allocated peak exceeds reservation"
    return allocated


def execute():
    """Stop at the first genuine notebook/controller failure, with capture and unlock in finally."""
    from held_reader import captures, digest
    from notebook_fixture import (
        commit,
        connection,
        pg,
        pg_connection,
        prepare,
        save,
        verify_capture,
    )

    report = {"status": "FAIL", "commands": [], "events": []}
    process = owner = observer = None
    try:
        verify_install()
        config = Path("/home/probe/.marimo.toml")
        assert not config.exists(), "Notebook config must be fresh"
        config.write_text(
            "[completion]\ncopilot = false\n[language_servers.pylsp]\nenabled = false\n"
        )
        key = secrets.randbelow(2**31 - 1) + 1
        binding = prepare(key, str(uuid.uuid4()))
        report["allocated_before_kernel"] = fixture_fit()
        report["binding"] = binding
        expected_hash = digest(Path("/work/notebook-binding.json").read_bytes())
        for case, expected_error in [
            ("stale", "REFUSE_STALE_BINDING"),
            ("catalog", "REFUSE_CATALOG_IDENTITY"),
        ]:
            process = cli(case, "0" * 64 if case == "stale" else expected_hash)
            assert finish(process, report, case) != 0, "Invalid notebook binding accepted"
            denied = json.loads(Path("/work/notebook-" + case + ".json").read_text())
            assert denied["analytical_query_started"] is False and denied["error"] == expected_error
            report[case] = denied
            stop_child(process)
            process = None
        owner, observer = pg_connection(), pg_connection()
        owner.execute("BEGIN")
        owner_pid = pg(owner, f"SELECT pg_backend_pid(),pg_advisory_xact_lock({key})::text")[0][0]
        process = cli("positive", expected_hash)
        deadline = time.monotonic() + 120
        while True:
            found = observe(observer, key, owner_pid)
            if found:
                break
            assert process.poll() is None, "Notebook exited before actual executed COPY witness"
            assert time.monotonic() < deadline, (
                "Missing notebook executed snapshot-before-wait witness"
            )
            time.sleep(0.1)
        started = json.loads(Path("/work/notebook-positive.json").read_text())
        assert started["analytical_query_started"] and started["status"] == "BOUND"
        assert os.getpgid(started["kernel_pid"]) == process.pid, "Kernel escaped owned child group"
        report["events"].append(
            {
                "event": "executed_copy_wait_before_commit",
                "monotonic": time.monotonic(),
                **found,
                "kernel_pid": started["kernel_pid"],
                "execution_uuid": binding["execution_uuid"],
                "sql_sha256": binding["sql_sha256"],
            }
        )
        report["commit"] = commit(binding)
        still = observe(observer, key, owner_pid)
        assert still == found and process.poll() is None, "Notebook admission changed during commit"
        report["events"].append(
            {"event": "coherent_commit_while_same_waiter_held", "monotonic": time.monotonic()}
        )
        unlock_start = time.monotonic()
        owner.execute("COMMIT")
        report["events"].append(
            {
                "event": "unlock",
                "requested_monotonic": unlock_start,
                "confirmed_monotonic": time.monotonic(),
            }
        )
        assert finish(process, report, "positive") == 0
        completed = json.loads(Path("/work/notebook-positive.json").read_text())
        assert completed["status"] == "PASS_REAL_MARIMO_HELD_350_FRESH_360"
        assert all(
            completed[k] == started[k]
            for k in ["kernel_pid", "kernel_start", "connection_identity"]
        )
        report["cells"] = completed
        assert (
            completed["executions"][0]["completed_monotonic"]
            >= report["events"][-1]["requested_monotonic"]
        )
        assert (
            completed["executions"][1]["started_monotonic"]
            >= completed["executions"][0]["completed_monotonic"]
        )
        verify_capture(binding["selected"])
        with connection() as con:
            assert captures(con) == binding["captures"]
        audit = [
            json.loads(line)
            for line in Path("/work/notebook-processes.jsonl").read_text().splitlines()
        ]
        assert audit and not any(r["refused"] for r in audit)
        assert any(
            r["event"] == "python_start" and r["pid"] == completed["kernel_pid"] for r in audit
        )
        report["process_audit"] = audit
        report["captured_selected_inputs_unchanged"] = True
        report["allocated_after_kernel"] = fixture_fit()
        report["spill_settings"] = [
            json.loads(line)
            for line in Path("/work/notebook-connections.jsonl").read_text().splitlines()
        ]
        report["status"] = STATUS
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        cleanup_errors = []
        try:
            stop_child(process)
        except Exception as error:
            cleanup_errors.append("child_cancel: " + str(error))
        if process is not None and not getattr(process, "receipted", False):
            try:
                finish(process, report, process.t44a_case, timeout=10)
            except Exception as error:
                report["child_capture_error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
                cleanup_errors.append("child_capture")
        for con in [owner, observer]:
            if con is not None:
                try:
                    con.close()  # Roll back/release any still-held transaction lock.
                except Exception as error:
                    cleanup_errors.append("connection_close: " + str(error))
        if cleanup_errors:
            report.update(status="FAIL", cleanup_errors=cleanup_errors)
        try:
            report["original_outputs"] = output_check()
        except Exception as error:
            report.update(status="FAIL", output_validation_error=str(error))
            raise
        finally:
            save("notebook-result.json", report)
        assert not cleanup_errors, "Notebook child/lock cleanup failed"


def export(rig, disk_gate):
    """Retain only bounded raw-checked JSON; HTML and package bytes never become public assets."""
    files = [
        rig.root / "work" / name
        for name in [
            "notebook-result.json",
            "notebook-binding.json",
            "notebook-positive.json",
            "notebook-stale.json",
            "notebook-catalog.json",
        ]
    ]
    disk_gate(
        rig, sum(p.stat().st_size for p in files if p.exists()), "before-notebook-evidence-export"
    )
    for path in files:
        if path.exists():
            data = path.read_bytes()
            assert len(data) <= 2 * MIB and not any(
                v.encode() in data for v in rig.secrets.values()
            )
            (rig.root / "evidence" / path.name).write_bytes(data)
    disk_gate(rig, 0, "after-notebook-evidence-export")


if __name__ == "__main__":
    execute()
