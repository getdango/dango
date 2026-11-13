"""tests/integration/t44a_qualification/dbt_publication.py

Scratch checked-publication and native analytical transaction primitives, not Dango integration.
"""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from dbt_assets import MIB, allocated

EXTRA_PEAK = 640 * MIB
TABLES = ("sales.orders", "finance.orders", "sales.summary", "main.release_marker")
MODELS = {
    "orders": ("sales", "orders"),
    "finance_orders": ("finance", "orders"),
    "summary": ("sales", "summary"),
}
TEST = "test.t44a_publication.accept_candidate"


def digest(value):
    """Identify complete synthetic logical rows without changing their evidence."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def inputs(project):
    """Bind the current project files, excluding only the ordinary log directory."""
    return {
        str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(project.rglob("*"))
        if p.is_file() and "logs" not in p.parts
    }


def artifacts(target):
    """Require all three untouched authentic dbt JSON artifacts."""
    return {
        name: hashlib.sha256((target / name).read_bytes()).hexdigest()
        for name in ("manifest.json", "run_results.json", "catalog.json")
    }


def resources(target):
    """Check the exact model/test set and native relation identities, not a supplied status."""
    manifest = json.loads((target / "manifest.json").read_text())
    rows = json.loads((target / "run_results.json").read_text())["results"]
    expected = {"model.t44a_publication." + n for n in MODELS} | {TEST}
    assert len(rows) == len(expected) and {r["unique_id"] for r in rows} == expected, (
        "Exact candidate model/test execution set required"
    )
    assert set(manifest["nodes"]) == expected, "Unexpected candidate manifest resource"
    catalog = json.loads((target / "catalog.json").read_text())["nodes"]
    assert set(catalog) == expected - {TEST}, "Exact candidate catalog models required"
    for name, (schema, alias) in MODELS.items():
        uid = "model.t44a_publication." + name
        node = manifest["nodes"][uid]
        assert (node["database"], node["schema"], node["alias"]) == ("lake", schema, alias)
        assert node["config"]["materialized"] == "table"
        native = catalog[uid]["metadata"]
        assert (native["database"], native["schema"], native["name"], native["type"].upper()) == (
            "lake",
            schema,
            alias,
            "BASE TABLE",
        )
    return {r["unique_id"]: {"status": r["status"], "failures": r.get("failures")} for r in rows}


def require_checked(receipt, project, target, captured, candidate):
    """Refuse failed, missing or stale evidence before opening an accepted writer."""
    assert receipt and receipt["exit"] == 0, "Missing or failed candidate check receipt"
    assert receipt["inputs"] == inputs(project), "Stale project check receipt"
    assert receipt["artifacts"] == artifacts(target), "Missing or stale authentic artifacts"
    assert receipt["captured_input"] == captured, "Stale captured input check receipt"
    assert receipt["candidate_rows"] == candidate, "Stale candidate output check receipt"
    actual = resources(target)
    assert receipt["resources"] == actual, "Stale resource status receipt"
    assert actual[TEST] == {"status": "pass", "failures": 0}, "Required test did not pass"
    assert all(actual["model.t44a_publication." + n]["status"] == "success" for n in MODELS)


def export_candidates(work):
    """Export only the small safe report and the two genuine dbt JSON sets."""
    result = [(work / "dbt-publication.json", "dbt-publication.json")]
    for phase in ("rejected", "checked"):
        result += [
            (work / "dbt-publication-stages" / phase / name, "publication-" + phase + "-" + name)
            for name in ("manifest.json", "run_results.json", "catalog.json")
        ]
    return result


def run_publication(table_project, metadata):
    """Exercise real failed checks, rollback, coherent commit and engine READ_ONLY denial."""
    import duckdb

    assert duckdb.__version__ == "2.0.0"
    assert "password=" not in metadata.lower(), "Only existing pgpass binding is permitted"
    work = Path("/work")
    project, stages = work / "dbt-publication-project", work / "dbt-publication-stages"
    data = {n: Path("/fixture/data/publication-" + n) for n in ("accepted", "candidate")}
    assert (
        not project.exists() and not stages.exists() and not any(p.exists() for p in data.values())
    )
    for path in [project / n for n in ("models", "macros", "tests")] + [stages, *data.values()]:
        path.mkdir(parents=True)
    report = {
        "status": "FAIL",
        "stages": {},
        "sql": [],
        "accepted_write_attempts": 0,
        "scope": "Scratch native primitives only; no integrated Dango publication or BI held-reader proof",
        "engine_package": duckdb.__version__,
    }
    secrets = list(json.loads(Path("/secrets/catalog.json").read_text()).values())

    def execute(connection, statement):
        entry = {"sql": statement}
        report["sql"].append(entry)
        try:
            result = connection.execute(statement)
            entry["status"] = "executed"
            return result
        except duckdb.Error as error:
            entry.update(status="engine_error", error_type=type(error).__name__, error=str(error))
            raise

    def connect(catalog="accepted", read_only=True):
        connection = duckdb.connect(config={"memory_limit": "512MB", "threads": "2"})
        try:
            connection.execute("LOAD postgres_scanner")
            if catalog == "original":
                connection.execute(
                    f"ATTACH 'ducklake:{metadata}' AS original (READ_ONLY, AUTOMATIC_MIGRATION FALSE)"
                )
            else:
                options = f"METADATA_SCHEMA 't44a_publish_{catalog}', DATA_PATH '{data[catalog]}', "
                options += (
                    "DUCKLAKE_VERSION '1.1', AUTOMATIC_MIGRATION FALSE, DATA_INLINING_ROW_LIMIT 0"
                )
                if read_only:
                    options += ", READ_ONLY"
                connection.execute(f"ATTACH 'ducklake:{metadata}' AS {catalog} ({options})")
            return connection
        except Exception:
            connection.close()
            raise

    def metadata_rows(query):
        with duckdb.connect(config={"memory_limit": "256MB"}) as connection:
            connection.execute("LOAD postgres_scanner")
            connection.execute(
                f"ATTACH '{metadata.removeprefix('postgres:')}' AS metadata_pg (TYPE POSTGRES)"
            )
            return connection.execute(
                "SELECT * FROM postgres_query('metadata_pg', $$" + query + "$$)"
            ).fetchall()

    def observe(catalog="accepted"):
        with connect(catalog) as connection:
            connection.execute("BEGIN")
            tables = TABLES if catalog == "accepted" else TABLES[:3]
            values = [
                f"(SELECT sum(amount) FROM {catalog}.sales.orders)",
                f"(SELECT sum(amount) FROM {catalog}.finance.orders)",
                f"(SELECT total FROM {catalog}.sales.summary)",
            ]
            if catalog == "accepted":
                values += ["(SELECT release_id FROM accepted.main.release_marker)"]
            value = execute(connection, "SELECT " + ",".join(values)).fetchone()
            rows = {
                t: execute(connection, f"SELECT * FROM {catalog}.{t} ORDER BY ALL").fetchall()
                for t in tables
            }
            state = metadata_rows(
                f"SELECT snapshot_id FROM t44a_publish_{catalog}.ducklake_snapshot ORDER BY snapshot_id",
            )
            identities = metadata_rows(
                f"SELECT schema_id,table_name,table_id,table_uuid::text FROM t44a_publish_{catalog}.ducklake_table "
                "WHERE end_snapshot IS NULL ORDER BY schema_id,table_name",
            )
            version = metadata_rows(
                f"SELECT value FROM t44a_publish_{catalog}.ducklake_metadata WHERE key='version'",
            )
            assert version == [("1.1",)] and len(identities) == len(tables) + (
                catalog == "candidate"
            )
            connection.execute("COMMIT")
        assert len(rows["sales.orders"]) == 3 and len({r[0] for r in rows["sales.orders"]}) == 3
        return {"tuple": list(value), "rows": rows, "snapshot_ids": state, "table_ids": identities}

    def candidate_binding():
        state = observe("candidate")
        assert state["tuple"] == [360, 8, 360]
        assert state["rows"]["sales.orders"] == [
            (r[0], r[1], r[2] + (10 if r[0] == 1 else 0)) for r in original
        ]
        assert state["rows"]["finance.orders"] == [(8,)] and state["rows"]["sales.summary"] == [
            (360,)
        ]
        with connect("candidate") as connection:
            raw = connection.execute("SELECT * FROM candidate.raw.orders ORDER BY ALL").fetchall()
        return digest(raw), digest(state["rows"])

    def allocation_gate():
        assert allocated(project) <= 32 * MIB, "Publication source/log bound"
        assert all(allocated(stages / p) <= 128 * MIB for p in ("rejected", "checked")), (
            "Publication target bound"
        )
        size = metadata_rows(
            "SELECT coalesce(sum(pg_total_relation_size(c.oid)),0) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','m') "
            "AND n.nspname IN ('t44a_publish_accepted','t44a_publish_candidate')",
        )[0][0]
        total = int(size) + sum(allocated(p) for p in data.values())
        report["data_and_metadata_upper"] = total
        assert total <= 64 * MIB, "Publication data/metadata bound; WAL remains globally accounted"

    def build(phase):
        target = stages / phase
        argv = [
            "/work/venv/bin/dbt",
            "build",
            "--project-dir",
            str(project),
            "--profiles-dir",
            str(project),
            "--target-path",
            str(target),
            "--write-catalog",
            "--no-version-check",
            "--no-send-anonymous-usage-stats",
        ]
        entry = report["stages"][phase] = {"command": argv, "inputs": inputs(project)}
        entry["exit"] = subprocess.run(argv, check=False).returncode
        assert entry["inputs"] == inputs(project), "Project inputs changed during dbt command"
        allocation_gate()
        entry["artifacts"], entry["resources"] = artifacts(target), resources(target)
        entry["captured_input"], entry["candidate_rows"] = candidate_binding()
        return entry

    def promote(receipt, fault=False):
        captured, candidate = candidate_binding()
        require_checked(receipt, project, stages / "checked", captured, candidate)
        report["accepted_write_attempts"] += 1
        connection, active = connect(read_only=False), False
        try:
            connection.execute(
                f"ATTACH 'ducklake:{metadata}' AS candidate (METADATA_SCHEMA 't44a_publish_candidate', READ_ONLY, AUTOMATIC_MIGRATION FALSE)"
            )
            execute(connection, "BEGIN")
            active = True
            for table in TABLES[:3]:
                # Candidate is a read-only source. Only accepted is modified in this transaction.
                execute(connection, f"DELETE FROM accepted.{table}")
                execute(connection, f"INSERT INTO accepted.{table} SELECT * FROM candidate.{table}")
                if fault:
                    partial = execute(
                        connection,
                        "SELECT (SELECT sum(amount) FROM accepted.sales.orders),"
                        "(SELECT sum(amount) FROM accepted.finance.orders),(SELECT total FROM accepted.sales.summary),"
                        "(SELECT release_id FROM accepted.main.release_marker)",
                    ).fetchone()
                    assert partial == (360, 7, 350, "r0")
                    report["writer_partial"], report["reader_during_fault"] = (
                        list(partial),
                        observe(),
                    )
                    assert report["reader_during_fault"] == report["accepted_baseline"]
                    try:
                        execute(
                            connection, "SELECT * FROM accepted.sales.injected_missing_relation"
                        )
                    except duckdb.CatalogException as error:
                        assert (
                            "injected_missing_relation" in str(error)
                            and "does not exist" in str(error).lower()
                        )
                        report["injected_fault"] = {
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                    else:
                        raise AssertionError("Native missing-relation fault did not occur")
                    execute(connection, "ROLLBACK")
                    active = False
                    return
            execute(connection, "UPDATE accepted.main.release_marker SET release_id='r1'")
            report["writer_before_commit"] = list(
                execute(
                    connection,
                    "SELECT (SELECT sum(amount) FROM accepted.sales.orders),(SELECT sum(amount) FROM accepted.finance.orders),"
                    "(SELECT total FROM accepted.sales.summary),(SELECT release_id FROM accepted.main.release_marker)",
                ).fetchone()
            )
            assert report["writer_before_commit"] == [360, 8, 360, "r1"]
            report["reader_before_commit"] = observe()
            assert report["reader_before_commit"] == report["accepted_baseline"]
            execute(connection, "COMMIT")
            active = False
        finally:
            try:
                if active:
                    execute(connection, "ROLLBACK")
            finally:
                connection.close()

    try:
        with connect("original") as source:
            original = source.execute("SELECT * FROM original.sales.orders ORDER BY ALL").fetchall()
        assert [(r[0], r[2]) for r in original] == [(1, 100), (2, 200), (3, 50)]
        report["original_orders"] = digest(original)
        for catalog in ("accepted", "candidate"):
            with connect(catalog, False) as connection:
                connection.execute(
                    f"ATTACH 'ducklake:{metadata}' AS original (READ_ONLY, AUTOMATIC_MIGRATION FALSE)"
                )
                if catalog == "candidate":
                    execute(connection, "CREATE SCHEMA candidate.raw")
                    execute(
                        connection,
                        "CREATE TABLE candidate.raw.orders AS SELECT * FROM original.sales.orders",
                    )
                else:
                    for schema in ("sales", "finance"):
                        execute(connection, f"CREATE SCHEMA accepted.{schema}")
                    execute(
                        connection,
                        "CREATE TABLE accepted.sales.orders AS SELECT * FROM original.sales.orders",
                    )
                    execute(
                        connection,
                        "CREATE TABLE accepted.finance.orders AS SELECT 7::INTEGER amount",
                    )
                    execute(
                        connection,
                        "CREATE TABLE accepted.sales.summary AS SELECT 350::BIGINT total",
                    )
                    execute(
                        connection,
                        "CREATE TABLE accepted.main.release_marker AS SELECT 'r0'::VARCHAR release_id",
                    )
        report["accepted_baseline"] = observe()
        assert report["accepted_baseline"]["tuple"] == [350, 7, 350, "r0"]
        assert report["accepted_baseline"]["rows"] == {
            "sales.orders": original,
            "finance.orders": [(7,)],
            "sales.summary": [(350,)],
            "main.release_marker": [("r0",)],
        }
        shutil.copyfile(table_project / "profiles.yml", project / "profiles.yml")
        catalogs = (table_project / "catalogs.yml").read_text()
        assert (
            catalogs.count("data_path: /fixture/data\n")
            == catalogs.count("catalog_database: lake\n")
            == 1
        )
        (project / "catalogs.yml").write_text(
            catalogs.replace(
                "data_path: /fixture/data\n", f"data_path: {data['candidate']}\n"
            ).replace(
                "catalog_database: lake\n",
                "catalog_database: lake\n        metadata_schema: t44a_publish_candidate\n",
            )
        )
        (project / "dbt_project.yml").write_text(
            "name: t44a_publication\nversion: '1.0'\nconfig-version: 2\nprofile: probe\n"
            "models:\n  t44a_publication:\n    +materialized: table\n    +catalog_name: lake\n    +database: lake\n"
        )
        (project / "macros/schema.sql").write_text(
            "{% macro generate_schema_name(custom_schema_name, node) -%}"
            "{{ custom_schema_name | trim if custom_schema_name else target.schema }}{%- endmacro %}\n"
        )
        (project / "models/orders.sql").write_text(
            "select id,order_date,amount + case when id=1 then 10 else 0 end amount from lake.raw.orders\n"
        )
        (project / "models/finance_orders.sql").write_text(
            "{{ config(schema='finance', alias='orders') }} select 8::INTEGER amount\n"
        )
        (project / "models/summary.sql").write_text(
            "select sum(amount) total from {{ ref('orders') }}\n"
        )
        test = project / "tests/accept_candidate.sql"
        check = "select 1 as failure where (select sum(amount) from {{ ref('orders') }}) <> EXPECTED or (select sum(amount) from {{ ref('finance_orders') }}) <> 8 or (select total from {{ ref('summary') }}) <> 360\n"
        test.write_text(check.replace("EXPECTED", "350"))
        rejected = build("rejected")
        assert rejected["exit"] != 0 and rejected["resources"][TEST]["status"] == "fail"
        assert rejected["resources"][TEST]["failures"] > 0
        assert all(
            rejected["resources"]["model.t44a_publication." + n]["status"] == "success"
            for n in MODELS
        )
        for label, receipt in (("failed", rejected), ("missing", None)):
            try:
                promote(receipt)
            except AssertionError as error:
                assert str(error) == "Missing or failed candidate check receipt"
                report.setdefault("refusals", {})[label] = str(error)
            else:
                raise AssertionError("Unchecked candidate was not refused")
        assert report["accepted_write_attempts"] == 0 and observe() == report["accepted_baseline"]
        test.write_text(check.replace("EXPECTED", "360"))
        assert [k for k, v in inputs(project).items() if rejected["inputs"].get(k) != v] == [
            "tests/accept_candidate.sql"
        ], "Only the deliberately failing test expectation may change"
        checked = build("checked")
        captured, candidate = candidate_binding()
        require_checked(checked, project, stages / "checked", captured, candidate)
        stale = dict(checked, captured_input="stale")
        try:
            promote(stale)
        except AssertionError as error:
            assert str(error) == "Stale captured input check receipt"
            report["refusals"]["stale"] = str(error)
        else:
            raise AssertionError("Stale candidate was not refused")
        assert report["accepted_write_attempts"] == 0 and observe() == report["accepted_baseline"]
        promote(checked, fault=True)
        report["after_rollback"] = observe()
        assert report["after_rollback"] == report["accepted_baseline"]
        promote(checked)
        report["after_commit"] = observe()
        assert report["after_commit"]["tuple"] == [360, 8, 360, "r1"]
        assert report["after_commit"]["rows"] == {
            **observe("candidate")["rows"],
            "main.release_marker": [("r1",)],
        }
        assert report["after_commit"]["table_ids"] == report["accepted_baseline"]["table_ids"]
        assert (
            len(report["after_commit"]["snapshot_ids"])
            == len(report["accepted_baseline"]["snapshot_ids"]) + 1
        )
        assert (
            report["after_commit"]["snapshot_ids"][:-1]
            == report["accepted_baseline"]["snapshot_ids"]
        )
        assert report["accepted_write_attempts"] == 2
        with connect() as connection:
            try:
                execute(connection, "UPDATE accepted.sales.orders SET amount=999 WHERE id=1")
            except duckdb.Error as error:
                message = str(error)
                denial = re.search(
                    r'Cannot execute statement of type "UPDATE" on database ("[^"]+"|\S+) which is attached in read-only mode!',
                    message,
                )
                assert denial and denial[1] in {"accepted", '"accepted"'}, (
                    "Native UPDATE denial must name the exact accepted database"
                )
                report["engine_readonly"] = {"type": type(error).__name__, "message": str(error)}
            else:
                raise AssertionError("Engine READ_ONLY UPDATE was not refused")
        assert observe() == report["after_commit"]
        with connect("original") as source:
            assert (
                digest(
                    source.execute("SELECT * FROM original.sales.orders ORDER BY ALL").fetchall()
                )
                == report["original_orders"]
            )
            assert source.execute(
                "SELECT sum(amount) FROM original.sales.orders_view"
            ).fetchone() == (350,)
            assert source.execute(
                "SELECT sum(amount) FROM original.sales.orders WHERE order_date >= DATE '2026-09-01' AND order_date < DATE '2026-10-01'"
            ).fetchone() == (300,)
        allocation_gate()
        report["status"] = "PASS_SCRATCH_CHECKED_PUBLICATION_AND_NATIVE_TRANSACTION"
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        payload = json.dumps(report, indent=2, default=str) + "\n"
        assert len(payload.encode()) <= 16 * MIB and not any(s in payload for s in secrets), (
            "Publication report size/secret refusal"
        )
        (work / "dbt-publication.json").write_text(payload)
