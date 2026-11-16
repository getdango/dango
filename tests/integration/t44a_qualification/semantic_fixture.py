"""tests/integration/t44a_qualification/semantic_fixture.py

Genuine dbt semantic fixtures and independent row/zoneinfo oracles, never query rewrites.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from semantic_oracles import CUSTOMERS, ROWS, independent_cases

MIB = 1024**2
EXTRA_PEAK = 832 * MIB
ARTIFACTS = ["manifest.json", "run_results.json", "catalog.json"]


def sha(data):
    """Hash exact bytes."""
    return hashlib.sha256(data).hexdigest()


def save(path, value, bound=4 * MIB):
    """Retain bounded synthetic reports only after the unchanged known-secret barrier."""
    data = json.dumps(value, indent=2, default=str).encode() + b"\n"
    known = json.loads(Path("/secrets/app.json").read_text()).values()
    assert len(data) <= bound and not any(v.encode() in data for v in known)
    path.write_bytes(data)


def literal(value):
    """Encode only the fixed synthetic fixture values, not user input."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value) if isinstance(value, int) else "'" + value.replace("'", "''") + "'"


def metadata():
    """Native supported metric/dimension/join shapes from the exact frozen schema."""

    def column(name, data_type, dimension_type, metrics=None):
        dimension = {"type": dimension_type}
        if dimension_type == "date":
            dimension["time_intervals"] = ["RAW", "DAY"]
        meta = {"dimension": dimension}
        if metrics:
            meta["metrics"] = {name: {"type": kind} for name, kind in metrics.items()}
        return {"name": name, "data_type": data_type, "config": {"meta": meta}}

    cols = [
        column("id", "INTEGER", "number", {"row_count": "count"}),
        column("customer_id", "VARCHAR", "string", {"customers": "count_distinct"}),
        column(
            "amount",
            "INTEGER",
            "number",
            {
                "total_amount": "sum",
                "amount_count": "count",
                "min_amount": "min",
                "max_amount": "max",
                "average_amount": "average",
            },
        ),
        column("units", "INTEGER", "number", {"total_units": "sum"}),
        column("category", "VARCHAR", "string"),
        column("active", "BOOLEAN", "boolean"),
    ]
    for label in ["utc", "singapore", "new_york"]:
        cols.append(column(label + "_date", "DATE", "date"))
    cols += [
        column("new_york_hour", "INTEGER", "number"),
        column("new_york_offset", "INTEGER", "number"),
    ]
    return {
        "version": 2,
        "models": [
            {
                "name": "semantic_events",
                "config": {
                    "meta": {
                        "primary_key": "id",
                        "metrics": {
                            "ratio": {
                                "type": "number",
                                "sql": "1.0 * ${total_amount} / NULLIF(${total_units}, 0)",
                            }
                        },
                        "joins": [
                            {
                                "join": "semantic_customers",
                                "type": "left",
                                "relationship": "many-to-one",
                                "sql_on": "${semantic_events.customer_id} = ${semantic_customers.customer_id}",
                            },
                            {
                                "join": "semantic_line_totals",
                                "type": "left",
                                "relationship": "one-to-one",
                                "sql_on": "${semantic_events.id} = ${semantic_line_totals.id}",
                            },
                        ],
                    }
                },
                "columns": cols,
            },
            {
                "name": "semantic_customers",
                "config": {"meta": {"primary_key": "customer_id"}},
                "columns": [
                    column("customer_id", "VARCHAR", "string"),
                    column("segment", "VARCHAR", "string"),
                ],
            },
            {
                "name": "semantic_line_totals",
                "config": {"meta": {"primary_key": "id"}},
                "columns": [
                    column("id", "INTEGER", "number"),
                    column("line_value", "INTEGER", "number", {"total_line_value": "sum"}),
                ],
            },
        ],
    }


def fixture_command(semantic):
    """Keep genuine original setup; semantic mode adds only its own model/test fixture."""
    return (
        "/work/venv/bin/python /scripts/diagnostic.py --held-reader-fixture"
        " && /work/venv/bin/python /scripts/producer_capture.py"
        " && /work/venv/bin/python /scripts/"
        + ("semantic_fixture.py prepare" if semantic else "held_reader.py prepare")
    )


def native_connection():
    """Bound the new fixture's native workspace and prohibit unaccounted spill."""
    from held_reader import connection

    directory = Path("/work/semantic-spill")
    directory.mkdir(mode=0o700, exist_ok=True)
    assert directory.is_dir() and not directory.is_symlink()
    con = connection()
    try:
        con.execute("SET temp_directory='/work/semantic-spill'")
        con.execute("SET max_temp_directory_size='0B'")
        assert con.execute(
            "SELECT current_setting('temp_directory'), current_setting('max_temp_directory_size')"
        ).fetchone() == (str(directory), "0 bytes")
        return con
    except BaseException:
        con.close()
        raise


def prepare():
    """Build real models and deliberately fail genuine dbt bad-key tests before serving."""
    from held_reader import captures

    project = Path("/work/project")
    report = {"status": "FAIL", "cases": independent_cases(), "stages": []}
    with native_connection() as con:
        original = captures(con)
    folder = Path("/work/semantic-artifacts")
    folder.mkdir()

    def run(stage, command, expected):
        args = [
            "/work/venv/bin/dbt",
            *command,
            "--project-dir",
            str(project),
            "--profiles-dir",
            str(project),
            "--no-version-check",
            "--no-send-anonymous-usage-stats",
        ]
        child = subprocess.run(
            args,
            capture_output=True,
            env={**os.environ, "ADBC_DRIVER_PATH": "/home/probe/adbc"},
            timeout=180,
        )
        known = json.loads(Path("/secrets/app.json").read_text()).values()
        assert len(child.stdout) + len(child.stderr) <= 8 * MIB
        assert not any(v.encode() in child.stdout + child.stderr for v in known)
        entry = {
            "stage": stage,
            "argv": args,
            "exit": child.returncode,
            "stdout": child.stdout.decode(),
            "stderr": child.stderr.decode(),
            "artifacts": {},
        }
        report["stages"].append(entry)
        for name in ARTIFACTS:
            p = project / "target" / name
            if p.exists():
                data = p.read_bytes()
                assert len(data) <= 32 * MIB and not any(v.encode() in data for v in known)
                target = folder / (stage + "-" + name)
                target.write_bytes(data)
                entry["artifacts"][target.name] = sha(data)
        assert child.returncode == expected, "Unexpected actual dbt semantic stage exit: " + stage
        return json.loads((project / "target/run_results.json").read_text())["results"]

    try:
        rows = ",\n".join("(" + ",".join(map(literal, row)) + ")" for row in ROWS)
        (project / "models/semantic_events.sql").write_text(
            "WITH inputs AS (SELECT id::INTEGER id,customer_id::VARCHAR customer_id,"
            "amount::INTEGER amount,units::INTEGER units,category::VARCHAR category,"
            "active::BOOLEAN active,event_at::TIMESTAMPTZ event_at FROM (VALUES\n"
            + rows
            + ") t(id,customer_id,amount,units,category,active,event_at)) SELECT *,"
            "(event_at AT TIME ZONE 'UTC')::DATE utc_date,"
            "(event_at AT TIME ZONE 'Asia/Singapore')::DATE singapore_date,"
            "(event_at AT TIME ZONE 'America/New_York')::DATE new_york_date,"
            "date_part('hour',event_at AT TIME ZONE 'America/New_York')::INTEGER new_york_hour,"
            "date_diff('second',event_at AT TIME ZONE 'UTC',"
            "event_at AT TIME ZONE 'America/New_York')::INTEGER new_york_offset FROM inputs\n"
        )
        customers = ",".join("(" + ",".join(map(literal, row)) + ")" for row in CUSTOMERS)
        models = {
            "semantic_customers": "SELECT * FROM (VALUES " + customers + ") t(customer_id,segment)",
            "semantic_line_totals": "SELECT id,sum(line_value)::INTEGER line_value FROM "
            "(VALUES (1,1),(1,2),(2,3)) t(id,line_value) GROUP BY id",
            "semantic_bad_customers": "SELECT * FROM {{ ref('semantic_customers') }} UNION ALL "
            "SELECT * FROM {{ ref('semantic_customers') }} WHERE customer_id='a'",
            "semantic_orphan_events": "SELECT 99::INTEGER id,'missing'::VARCHAR customer_id",
        }
        for name, sql in models.items():
            (project / "models" / (name + ".sql")).write_text(sql + "\n")
        (project / "models/semantic_schema.yml").write_text(json.dumps(metadata(), indent=2))
        tests = {
            "semantic_duplicate_parent": "SELECT customer_id FROM {{ ref('semantic_bad_customers') }} "
            "GROUP BY customer_id HAVING count(*)>1",
            "semantic_orphan_parent": "SELECT o.id FROM {{ ref('semantic_orphan_events') }} o "
            "LEFT JOIN {{ ref('semantic_customers') }} c USING(customer_id) WHERE c.customer_id IS NULL",
        }
        for name, sql in tests.items():
            (project / "tests" / (name + ".sql")).write_text(sql + "\n")
        results = run("models", ["run", "--select", "semantic_*", "--write-catalog"], 0)
        assert len(results) == 5 and all(r["status"] == "success" for r in results)
        results = run("bad-keys", ["test", "--select", *tests], 1)
        assert {r["unique_id"] for r in results} == {"test.t44a_probe." + n for n in tests}
        assert all(r["status"] == "fail" and r["failures"] == 1 for r in results)
        (project / "tests/semantic_valid_keys.sql").write_text(
            "SELECT e.id FROM {{ ref('semantic_events') }} e LEFT JOIN "
            "{{ ref('semantic_customers') }} c USING(customer_id) "
            "WHERE e.customer_id IS NOT NULL AND c.customer_id IS NULL UNION ALL "
            "SELECT 0 FROM {{ ref('semantic_customers') }} GROUP BY customer_id HAVING count(*)<>1 "
            "UNION ALL SELECT id FROM {{ ref('semantic_line_totals') }} GROUP BY id HAVING count(*)<>1\n"
        )
        results = run("valid-keys", ["test", "--select", "semantic_valid_keys"], 0)
        assert len(results) == 1 and results[0]["status"] == "pass" and results[0]["failures"] == 0
        with native_connection() as con:
            checks = [
                (
                    "SELECT customer_id,count(*) FROM lake.sales.semantic_bad_customers "
                    "GROUP BY customer_id HAVING count(*)>1",
                    [("a", 2)],
                ),
                (
                    "SELECT o.id FROM lake.sales.semantic_orphan_events o LEFT JOIN "
                    "lake.sales.semantic_customers c USING(customer_id) WHERE c.customer_id IS NULL",
                    [(99,)],
                ),
                (
                    "SELECT customer_id FROM lake.sales.semantic_customers "
                    "GROUP BY customer_id HAVING count(*)<>1",
                    [],
                ),
                (
                    "SELECT e.id FROM lake.sales.semantic_events e LEFT JOIN "
                    "lake.sales.semantic_customers c USING(customer_id) "
                    "WHERE e.customer_id IS NOT NULL AND c.customer_id IS NULL",
                    [],
                ),
                ("SELECT sum(amount) FROM lake.sales.orders", [(350,)]),
            ]
            report["native_key_checks"] = []
            for sql, expected in checks:
                actual = con.execute(sql).fetchall()
                report["native_key_checks"].append(
                    {"sql": sql, "actual": actual, "expected": expected}
                )
                assert actual == expected, "Native semantic key/control observation mismatch"
            assert captures(con) == original
        report.update(
            status="PASS_REAL_SEMANTIC_FIXTURE_AND_BAD_KEY_REFUSALS",
            captures=original,
            fixture_rows=ROWS,
            customers=CUSTOMERS,
            source_sha256={
                str(p.relative_to(project)): sha(p.read_bytes())
                for folder in ["models", "tests"]
                for p in (project / folder).glob("semantic_*")
            },
            calendar_policy="Explicit named IANA business-calendar DATE/hour/offset columns "
            "from aware instants in real dbt SQL; not session-timezone feature qualification",
        )
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        save(Path("/work/semantic-fixture.json"), report, 20 * MIB)
        allocated = sum(
            p.stat().st_blocks * 512 for base in [folder, project] for p in [base, *base.rglob("*")]
        )
        assert allocated <= 416 * MIB, "Semantic project/artifact allocation bound"


def reserve(root):
    """Charge all future semantic copies before acquisition and offline installation."""
    mode = json.loads((root / "evidence/result.json").read_text()).get("qualification_mode")
    return EXTRA_PEAK if mode == "semantic" else 0


def run_actor(rig, name, app_name, install, result):
    """Use the original launch/identity guards and demand invariant PG/Parquet after real queries."""
    from held_reader import pg_snapshot

    rig.start_owned(name)
    completed = rig.call(["docker", "wait", rig.objects[name][1]["Id"]], timeout=600)
    assert completed.stdout.strip() == "0", (
        "Semantic actor failed; retained native receipts required"
    )
    proof = json.loads((rig.root / "work/app-slice.json").read_text())
    assert proof["status"] == "PASS_SEMANTIC_MATRIX_FULL_APP"
    result["catalog_after"] = pg_snapshot(rig)
    assert result["catalog_before"] == result["catalog_after"], (
        "Semantic queries mutated catalog/data"
    )
    result["semantic_matrix"] = proof["semantic"]["status"]


def export(rig, generated, disk_gate):
    """Charge actual extra exported bytes and preserve full original authentic artifact bytes."""
    files = [rig.root / "work/semantic-fixture.json"]
    folder = rig.root / "work/semantic-artifacts"
    if folder.exists():
        files += sorted(folder.glob("*.json"))
    assert sum(p.stat().st_size for p in files if p.exists()) <= 308 * MIB
    disk_gate(
        rig, sum(p.stat().st_size for p in files if p.exists()), "before-semantic-report-export"
    )
    for p in files:
        if p.exists():
            data = p.read_bytes()
            assert not any(v.encode() in data for v in generated.values())
            (rig.root / "evidence" / ("semantic-" + p.name)).write_bytes(data)
    disk_gate(rig, 0, "after-semantic-report-export")


if __name__ == "__main__":
    assert sys.argv[1:] == ["prepare"]
    prepare()
