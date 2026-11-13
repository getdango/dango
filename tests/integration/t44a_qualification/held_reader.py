"""tests/integration/t44a_qualification/held_reader.py

Pinned synthetic held-reader branch; native results are never inferred from a wait alone.
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

MIB = 1024**2
EXTRA_PEAK = 64 * MIB
PREREQUISITE = "PASS_HELD_DBT_AND_SOURCE_PINNED_PRODUCER_PREREQUISITES"
DATA = Path("/work/held-data")
METADATA = "postgres:host=127.0.0.1 port=5432 dbname=probe user=postgres"
GATE_SQL = """CREATE FUNCTION public.t44a_held_gate(k bigint) RETURNS integer
LANGUAGE plpgsql VOLATILE AS $$ BEGIN
PERFORM pg_advisory_xact_lock(k); RETURN 0; END $$"""


def prerequisite(directory, target):
    """Require both actual stage reports, pinned source identity and authentic dbt files."""
    from dlt_source import PINS as producer_pins

    expected = {
        "diagnostic.json": "PASS_DBT_DRIVER_DIAGNOSTIC",
        "producer-capture.json": "PASS_SOURCE_PINNED_DLT_DUCKLAKE_CAPTURE",
        "dlt-install.json": "PASS_EXACT_UPSTREAM_SOURCE_AND_OFFLINE_CLOSURE",
    }
    reports, bindings = {}, {}
    for name, status in expected.items():
        data = (directory / name).read_bytes()
        report = json.loads(data)
        assert report["status"] == status, "Held prerequisite failed: " + name
        reports[name] = report
        bindings[name] = {"status": status, "sha256": digest(data)}
    assert reports["producer-capture.json"]["source_commit"] == producer_pins["source_commit"]
    assert reports["dlt-install.json"]["commit"] == producer_pins["source_commit"]
    assert reports["dlt-install.json"]["source_archive_sha256"] == producer_pins["source"]["sha256"]
    artifacts = reports["diagnostic.json"]["dbtArtifacts"]
    assert set(artifacts) == {"manifest.json", "run_results.json", "catalog.json"}
    assert all(digest((target / n).read_bytes()) == value for n, value in artifacts.items())
    return {
        "status": PREREQUISITE,
        "reports": bindings,
        "dbt_artifacts": artifacts,
        "source_commit": producer_pins["source_commit"],
    }


def require_acquisition_prerequisite(root):
    """Keep ordinary diagnostic admission and accept only the exact bound held composite."""
    result = json.loads((root / "evidence/result.json").read_text())
    if result["status"] == "PASS_DBT_DRIVER_DIAGNOSTIC":
        return
    assert result["status"] == PREREQUISITE, "Prior real native diagnostic/composite must pass"
    assert result["held_prerequisite"] == prerequisite(root / "evidence", root / "evidence"), (
        "Missing or stale held prerequisite bindings"
    )


def artifact_union(base, producer):
    """Combine only existing frozen public closures; reject conflicting shared bytes."""
    combined = {}
    for pin in [*base["artifacts"], *producer]:
        name = pin["file"]
        if name in combined:
            old = combined[name]
            assert (old["sha256"], old["compressed"]) == (pin["sha256"], pin["compressed"])
            combined[name] = {
                **old,
                "expanded_upper": max(old["expanded_upper"], pin["expanded_upper"]),
            }
        else:
            combined[name] = pin
    return list(combined.values())


def install_gate(rig):
    """Create only the owned synthetic gate with the normal authenticated PG CLI."""
    rig.call(
        [
            "docker",
            "exec",
            rig.pg,
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            "postgres",
            "-d",
            "probe",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            GATE_SQL,
        ]
    )


def digest(data):
    """Bind original bytes, never a rewritten artifact."""
    return hashlib.sha256(data).hexdigest()


def save(name, value):
    """Bound and raw-check every new allowlisted report before writing."""
    data = json.dumps(value, sort_keys=True, default=str, indent=2).encode()
    known = json.loads(Path("/secrets/app.json").read_text()).values()
    assert len(data) <= 2 * MIB and not any(v.encode() in data for v in known)
    Path("/work", name).write_bytes(data + b"\n")


def connection():
    """Use the already-frozen actual Python engine and signed extensions."""
    import duckdb

    assert duckdb.__version__ == "2.0.0"
    os.environ["PGPASSFILE"] = "/secrets/pgpass-python"
    con = duckdb.connect(config={"memory_limit": "128MB", "threads": "1"})
    con.execute("LOAD postgres_scanner")
    con.execute("LOAD ducklake")
    con.execute(
        f"ATTACH 'ducklake:{METADATA}' AS lake (DATA_PATH '{DATA}', "
        "AUTOMATIC_MIGRATION FALSE, DATA_INLINING_ROW_LIMIT 0)"
    )
    return con


def captures(con):
    """Bind complete metadata rows and every captured/selected physical file."""
    result = {}
    con.execute(f"ATTACH '{METADATA.removeprefix('postgres:')}' AS witness_pg (TYPE POSTGRES)")
    try:
        for name in ["capture", "actor"]:
            schema = "t44a_producer_" + name
            tables = con.execute(
                "SELECT * FROM postgres_query('witness_pg', $$SELECT table_name FROM "
                f"information_schema.tables WHERE table_schema='{schema}' ORDER BY table_name$$)"
            ).fetchall()
            assert tables
            rows = {}
            for (table,) in tables:
                assert re.fullmatch(r"[a-z_]+", table)
                values = con.execute(
                    "SELECT * FROM postgres_query('witness_pg', $$SELECT row_to_json(t)::text "
                    f"FROM {schema}.{table} t$$)"
                ).fetchall()
                rows[table] = digest(json.dumps(sorted(r[0] for r in values)).encode())
            root = Path("/fixture/data/producer-" + name)
            files = {
                str(p.relative_to(root)): digest(p.read_bytes()) for p in root.rglob("*.parquet")
            }
            assert files
            result[name] = {"metadata": rows, "files": files}
    finally:
        con.execute("DETACH witness_pg")
    return result


def fixture_fit():
    """Keep physical extra branch/captured data within the declared64MiB reservation."""
    roots = [DATA, *[Path("/fixture/data/producer-" + n) for n in ["raw", "capture", "actor"]]]
    allocated = sum(
        p.stat().st_blocks * 512 for root in roots for p in [root, *root.rglob("*")] if p.exists()
    )
    assert allocated <= EXTRA_PEAK - 12 * MIB, "Held fixture exceeds reserved peak"
    return allocated


def state(con):
    """Independently observe the coherent analytical tuple and snapshot identity."""
    rows = con.execute("SELECT id,amount FROM lake.sales.orders ORDER BY id").fetchall()
    assert len(rows) == 3 and [r[0] for r in rows] == [1, 2, 3]
    return {
        "rows": rows,
        "total": sum(r[1] for r in rows),
        "finance": con.execute("SELECT amount FROM lake.finance.orders").fetchone()[0],
        "summary": con.execute("SELECT amount FROM lake.sales.summary").fetchone()[0],
        "release": con.execute("SELECT revision FROM lake.main.release_marker").fetchone()[0],
        "snapshot": con.execute("SELECT id FROM ducklake_current_snapshot('lake')").fetchone()[0],
    }


def prepare():
    """Build genuine dbt view metadata over a branch copied from captured350 inputs."""
    project = Path("/work/project")
    key = secrets.randbelow(2**31 - 1) + 1
    with connection() as con:
        con.execute(
            f"ATTACH 'ducklake:{METADATA}' AS selected (METADATA_SCHEMA 't44a_producer_actor', "
            "DATA_PATH '/fixture/data/producer-actor', READ_ONLY, AUTOMATIC_MIGRATION FALSE)"
        )
        assert con.execute("SELECT sum(amount) FROM selected.sales.orders").fetchone() == (350,)
        con.execute("BEGIN")
        con.execute("DELETE FROM lake.sales.orders")
        con.execute(
            "INSERT INTO lake.sales.orders SELECT id,order_date,amount FROM selected.sales.orders"
        )
        con.execute("CREATE SCHEMA lake.finance")
        con.execute("CREATE TABLE lake.finance.orders AS SELECT 7::INTEGER amount")
        con.execute("CREATE TABLE lake.sales.summary AS SELECT 350::INTEGER amount")
        con.execute("CREATE TABLE lake.main.release_marker AS SELECT 'r0'::VARCHAR revision")
        con.execute("COMMIT")
    sql = (
        "{{ config(materialized='view') }}\n"
        "SELECT o.id,o.order_date,o.amount+g.zero AS amount,s.id AS snapshot_id "
        "FROM {{ ref('orders') }} o CROSS JOIN ducklake_current_snapshot('lake') s "
        "CROSS JOIN postgres_query('__ducklake_metadata_lake', "
        f"'SELECT public.t44a_held_gate({key}) AS zero') g\n"
    )
    (project / "models/held_orders.sql").write_text(sql)
    (project / "models/held_schema.yml").write_text(
        "version: 2\nmodels:\n  - name: held_orders\n    columns:\n"
        "      - name: amount\n        data_type: INTEGER\n        config:\n          meta:\n"
        "            metrics:\n              total_amount:\n                type: sum\n"
        "      - name: snapshot_id\n        data_type: UBIGINT\n        config:\n          meta:\n"
        "            metrics:\n              snapshot_marker:\n                type: max\n"
    )
    subprocess.run(
        [
            "/work/venv/bin/dbt",
            "build",
            "--select",
            "held_orders",
            "--write-catalog",
            "--project-dir",
            str(project),
            "--profiles-dir",
            str(project),
            "--no-version-check",
            "--no-send-anonymous-usage-stats",
        ],
        check=True,
    )
    manifest = json.loads((project / "target/manifest.json").read_text())
    assert manifest["nodes"]["model.t44a_probe.held_orders"]["config"]["materialized"] == "view"
    results = json.loads((project / "target/run_results.json").read_text())["results"]
    assert len(results) == 1 and results[0]["unique_id"] == "model.t44a_probe.held_orders"
    assert results[0]["status"] == "success"
    with connection() as con:
        before = state(con)
        assert (before["total"], before["finance"], before["summary"], before["release"]) == (
            350,
            7,
            350,
            "r0",
        )
        original = captures(con)
    fixture_fit()
    save(
        "held-binding.json",
        {
            "key": key,
            "before": before,
            "captures": original,
            "view_sql_sha256": digest(sql.encode()),
            "dbt_artifacts": {
                n: digest((project / "target" / n).read_bytes())
                for n in ["manifest.json", "run_results.json", "catalog.json"]
            },
        },
    )


def commit():
    """Exactly one real transaction; immutable captured inputs must remain identical."""
    binding = json.loads(Path("/work/held-binding.json").read_text())
    with connection() as con:
        before = state(con)
        assert json.loads(json.dumps(before)) == binding["before"], (
            "Branch advanced before held admission"
        )
        assert captures(con) == binding["captures"]
        sql = [
            "BEGIN",
            "UPDATE lake.sales.orders SET amount=210 WHERE id=2",
            "UPDATE lake.finance.orders SET amount=8",
            "UPDATE lake.sales.summary SET amount=360",
            "UPDATE lake.main.release_marker SET revision='r1'",
            "COMMIT",
        ]
        for query in sql:
            con.execute(query)
        after = state(con)
        assert (after["total"], after["finance"], after["summary"], after["release"]) == (
            360,
            8,
            360,
            "r1",
        )
        assert after["snapshot"] == before["snapshot"] + 1
        assert captures(con) == binding["captures"], "Captured/selected350 inputs changed"
    fixture_fit()
    save(
        "held-native.json",
        {
            "status": "PASS_COHERENT_NATIVE_COMMIT",
            "sql": sql,
            "before": before,
            "after": after,
            "captured_inputs_unchanged": True,
        },
    )
    print("PASS_COHERENT_NATIVE_COMMIT")


def pg_snapshot(rig):
    """Preserve the existing whole-catalog/read-only invariance check before mutation."""
    raw = rig.call(
        [
            "docker",
            "exec",
            rig.pg,
            "pg_dump",
            "--host=127.0.0.1",
            "--username=postgres",
            "--dbname=probe",
            "--no-owner",
            "--no-acl",
        ]
    ).stdout
    normalized = "\n".join(
        line
        for line in raw.splitlines()
        if not re.fullmatch(r"\\(?:un)?restrict [A-Za-z0-9]+", line)
    )
    files = {}
    for relative in ["fixture/data", "work/held-data"]:
        root = rig.root / relative
        files.update(
            {
                relative + "/" + str(p.relative_to(root)): digest(p.read_bytes())
                for p in root.rglob("*.parquet")
            }
        )
    assert files
    return {"catalog_dump_sha256": digest(normalized.encode()), "parquet": files}


def export_reports(rig, generated, disk_gate):
    """Measure both evidence copies and retain the existing raw-secret export barrier."""
    files = [
        rig.root / "work" / n for n in ["held-binding.json", "held-native.json", "held-reader.json"]
    ]
    disk_gate(rig, sum(p.stat().st_size for p in files if p.exists()), "before-held-report-export")
    for candidate in files:
        if candidate.exists():
            data = candidate.read_bytes()
            assert len(data) <= 2 * MIB and not any(v.encode() in data for v in generated.values())
            (rig.root / "evidence" / candidate.name).write_bytes(data)
    disk_gate(rig, 0, "after-held-report-export")


def run_actor(rig, name, install, result):
    """Verify read-only invariance before authorizing deliberate branch mutation."""
    app_name = next(n for n, (component, _) in rig.objects.items() if component == "app")

    def identity():
        info = json.loads(rig.call(["docker", "inspect", app_name]).stdout)[0]
        assert info["Id"] == rig.objects[app_name][1]["Id"] and info["State"]["Running"]
        return {
            "id": info["Id"],
            "image": info["Image"],
            "pid": info["State"]["Pid"],
            "started_at": info["State"]["StartedAt"],
            "restart_count": info["RestartCount"],
        }

    before = identity()
    rig.start_owned(name)
    deadline = time.monotonic() + 180
    while not (rig.root / "work/held-baseline-ready").exists():
        info = json.loads(rig.call(["docker", "inspect", name]).stdout)[0]
        assert info["Id"] == rig.objects[name][1]["Id"] and info["State"]["Running"], (
            "Actor exited before read-only baseline; normal cleanup retains capture"
        )
        assert time.monotonic() < deadline, "Actor read-only baseline deadline"
        time.sleep(0.25)
    result["catalog_after"] = pg_snapshot(rig)
    assert result["catalog_before"] == result["catalog_after"], "Read-only catalog/parquet changed"
    result["read_only_baseline_verified_before_commit"] = True
    install(rig.root / "source/readiness", rig.root / "work/held-baseline-verified")
    completed = rig.call(["docker", "wait", rig.objects[name][1]["Id"]], timeout=600)
    assert completed.stdout.strip() == "0", "Held actor failed; native receipts retained"
    held = json.loads((rig.root / "work/held-reader.json").read_text())
    assert held["status"] == "PASS_HELD_350_FRESH_360_FULL_APP"
    after = identity()
    assert before == after, "Full app restarted or changed across held/fresh queries"
    result["held_reader"] = {
        "status": held["status"],
        "same_app": before,
        "held_query_uuid": held["held"]["queryUuid"],
        "fresh_query_uuid": held["fresh"]["queryUuid"],
    }


if __name__ == "__main__":
    {"prepare": prepare, "commit": commit}[sys.argv[1]]()
