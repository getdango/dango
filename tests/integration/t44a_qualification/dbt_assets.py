"""tests/integration/t44a_qualification/dbt_assets.py

Remaining early native dbt assets, using only the frozen ordinary OSS route.
"""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

STAGES = (
    "seed",
    "incremental-first",
    "snapshot-first",
    "incremental-second",
    "snapshot-second",
    "docs",
    "python",
)
MIB = 1024**2
EXTRA_PEAK = 2048 * MIB


def allocated(path):
    """Conservatively count payload blocks and per-entry filesystem overhead."""
    return sum(
        ((p.stat().st_size + 4095) // 4096 + 1) * 4096 if p.is_file() else 4096
        for p in path.rglob("*")
    )


def export_candidates(work):
    """Name only genuine stage JSONs and the selected native/docs Parquet evidence."""
    result = [(work / "dbt-assets.json", "dbt-assets.json")]
    for stage in STAGES:
        target = work / "dbt-assets-stages" / stage
        result += [
            (target / name, "assets-" + stage + "-" + name)
            for name in ["manifest.json", "run_results.json", "catalog.json"]
        ]
        tables = ["dbt_rt.adapter_queries.parquet"]
        if stage == "docs":
            tables += ["dbt.models.parquet", "dbt.project.parquet"]
        for name in tables:
            matches = list(target.glob("info_schema/v*/" + name))
            assert len(matches) <= 1, "Ambiguous native stage artifact"
            result += [(path, "assets-" + stage + "-" + name) for path in matches]
    return result


def executed_incremental_sql(path):
    """Read completed native query spans from the pinned CLI's local JSONL sink."""
    sql = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if (
            event["record_type"] != "SpanEnd"
            or event["event_type"] != "v1.public.events.fusion.query.QueryExecuted"
        ):
            continue
        attributes = event["attributes"]
        if attributes.get("unique_id") != "model.t44a_probe.asset_incremental":
            continue
        assert attributes["query_outcome"] == "QUERY_OUTCOME_SUCCESS", (
            "Selected incremental native query did not complete successfully"
        )
        statement = attributes["sql"]
        assert isinstance(statement, str) and statement.strip() and statement != "<redacted>", (
            "Selected incremental native SQL is missing or redacted"
        )
        sql.append(statement)
    assert sql, "No completed selected-model native queries in explicit CLI trace"
    return sql


def run_assets(table_project, metadata):
    """Run real incremental/history/docs and precisely classify Python capability."""
    import duckdb

    work = Path("/work")
    project = work / "dbt-assets-project"
    stages = work / "dbt-assets-stages"
    assert not project.exists() and not stages.exists(), "Assets project must be fresh"
    for folder in ["models", "seeds", "snapshots"]:
        (project / folder).mkdir(parents=True, exist_ok=True)
    stages.mkdir()
    for name in ["dbt_project.yml", "catalogs.yml"]:
        shutil.copyfile(table_project / name, project / name)
    profile = (table_project / "profiles.yml").read_text()
    assert profile.count("schema: sales") == 1
    (project / "profiles.yml").write_text(profile.replace("schema: sales", "schema: assets"))
    (project / "seeds/asset_input.csv").write_text(
        "id,order_date,amount\n1,2026-09-01,100\n2,2026-09-15,200\n3,2026-10-01,50\n"
    )
    (project / "models/asset_incremental.sql").write_text(
        "{{ config(materialized='incremental', unique_key='id', incremental_strategy='delete+insert') }}\n"
        "select *, {{ 'true' if is_incremental() else 'false' }} as via_incremental "
        "from {{ ref('asset_input') }}\n"
    )
    (project / "models/schema.yml").write_text(
        "version: 2\nmodels:\n  - name: asset_incremental\n"
        "    description: Native incremental qualification asset\n"
    )
    (project / "snapshots/asset_history.sql").write_text(
        "{% snapshot asset_history %}\n"
        "{{ config(catalog_name='lake', database='lake', unique_key='id', strategy='check', check_cols=['amount']) }}\n"
        "select * from {{ ref('asset_input') }}\n{% endsnapshot %}\n"
    )
    report = {"status": "FAIL", "stages": {}, "safe_parquet": {}}
    secret_values = list(json.loads(Path("/secrets/catalog.json").read_text()).values())
    common = [
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(project),
        "--no-version-check",
        "--no-send-anonymous-usage-stats",
    ]

    def connect(read_only=True):
        connection = duckdb.connect(config={"memory_limit": "512MB", "threads": "2"})
        options = (
            "READ_ONLY, AUTOMATIC_MIGRATION FALSE" if read_only else "AUTOMATIC_MIGRATION FALSE"
        )
        connection.execute(f"ATTACH 'ducklake:{metadata}' AS lake ({options})")
        return connection

    def command(stage, args, expected_uid=None, allow_failure=False):
        report["current_stage"] = stage
        assert allocated(project) <= 64 * MIB, "Assets source/log allocation bound"
        target = stages / stage
        argv = ["/work/venv/bin/dbt", *args, *common, "--target-path", str(target)]
        if stage == "docs":
            argv = [
                "/work/venv/bin/dbt",
                "docs",
                *common,
                "generate",
                "--target-path",
                str(target),
                "--output-dir",
                str(target),
            ]
        else:
            argv += ["--write-catalog", "--generate-info-schema"]
        if stage == "incremental-second":
            argv += [
                "--log-path",
                str(target / "logs"),
                "--otel-file-name",
                "executed.jsonl",
                "--log-level-file",
                "debug",
            ]
        entry = report["stages"][stage] = {
            "command": argv,
            "inputs": {
                str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in project.rglob("*")
                if p.is_file() and "logs" not in p.parts
            },
        }
        result = subprocess.run(argv, check=False)
        entry["exit"] = result.returncode
        entry["allocated_upper"] = {
            "target": allocated(target),
            "project_logs": allocated(project),
            "fixture_data": allocated(Path("/fixture/data")),
        }
        assert entry["allocated_upper"]["target"] <= 128 * MIB, "Stage target allocation bound"
        assert entry["allocated_upper"]["project_logs"] <= 64 * MIB, (
            "Assets source/log allocation bound"
        )
        assert entry["allocated_upper"]["fixture_data"] <= 128 * MIB, (
            "Tiny native fixture allocation bound"
        )
        if not allow_failure:
            assert result.returncode == 0, (
                "Native assets command failed; authentic outputs retained"
            )
        if expected_uid:
            manifest = json.loads((target / "manifest.json").read_text())
            results = json.loads((target / "run_results.json").read_text())["results"]
            assert len(results) == 1 and results[0]["unique_id"] == expected_uid
            if not allow_failure:
                assert results[0]["status"] == "success"
            node = manifest["nodes"][expected_uid]
            assert (node["database"], node["schema"]) == ("lake", "assets")
            return target, node, results[0]
        return target

    def table_identity(name):
        with connect() as connection:
            connection.execute("LOAD postgres_scanner")
            connection.execute(
                f"ATTACH '{metadata.removeprefix('postgres:')}' AS metadata_pg (TYPE POSTGRES)"
            )
            rows = connection.execute(
                "SELECT * FROM postgres_query('metadata_pg', "
                "$$SELECT table_id, table_uuid::text FROM ducklake_table "
                f"WHERE table_name='{name}' AND end_snapshot IS NULL$$)"
            ).fetchall()
            assert len(rows) == 1
            return rows[0]

    def incremental_rows(total, via_incremental):
        with connect() as connection:
            rows = connection.execute(
                "SELECT id, amount, via_incremental FROM lake.assets.asset_incremental ORDER BY id"
            ).fetchall()
            assert len(rows) == 3 and len({r[0] for r in rows}) == 3
            assert sum(r[1] for r in rows) == total and all(r[2] is via_incremental for r in rows)
            return rows

    def history_rows(expected):
        with connect() as connection:
            rows = connection.execute(
                "SELECT id, amount, dbt_valid_to IS NULL FROM lake.assets.asset_history ORDER BY id, amount"
            ).fetchall()
            assert rows == expected
            assert connection.execute(
                "SELECT count(*) FROM lake.assets.asset_history WHERE dbt_valid_from IS NULL"
            ).fetchone() == (0,)
            return rows

    try:
        command("seed", ["seed", "--select", "asset_input"], "seed.t44a_probe.asset_input")
        _, node, _ = command(
            "incremental-first",
            ["build", "--select", "asset_incremental"],
            "model.t44a_probe.asset_incremental",
        )
        assert node["config"]["materialized"] == "incremental"
        assert node["config"]["incremental_strategy"] == "delete+insert"
        report["incremental_before"] = incremental_rows(350, False)
        inc_identity = table_identity("asset_incremental")
        report["table_identities"] = {"incremental_before": inc_identity}
        command(
            "snapshot-first",
            ["snapshot", "--select", "asset_history"],
            "snapshot.t44a_probe.asset_history",
        )
        report["snapshot_before"] = history_rows([(1, 100, True), (2, 200, True), (3, 50, True)])
        snap_identity = table_identity("asset_history")
        report["table_identities"]["snapshot_before"] = snap_identity
        with connect(False) as connection:
            connection.execute("UPDATE lake.assets.asset_input SET amount=210 WHERE id=2")
            assert connection.execute(
                "SELECT count(*),sum(amount) FROM lake.assets.asset_input"
            ).fetchone() == (3, 360)
        target, node, _ = command(
            "incremental-second",
            ["build", "--select", "asset_incremental"],
            "model.t44a_probe.asset_incremental",
        )
        assert (
            node["config"]["materialized"] == "incremental"
            and node["config"]["incremental_strategy"] == "delete+insert"
        )
        report["table_identities"]["incremental_after"] = table_identity("asset_incremental")
        assert report["table_identities"]["incremental_after"] == inc_identity, (
            "Incremental target was replaced"
        )
        report["incremental_after"] = incremental_rows(360, True)
        trace = target / "logs/executed.jsonl"
        report["incremental_sql_evidence"] = {
            "source": "Pinned CLI QueryExecuted SpanEnd JSONL (not compiled SQL)",
            "sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            "bytes": trace.stat().st_size,
            "raw_trace_exported": False,
        }
        sql = executed_incremental_sql(trace)
        report["incremental_native_sql"] = sql
        text = re.sub(r"\s+", " ", " ".join(sql).replace('"', "")).lower()
        assert (
            "delete from lake.assets.asset_incremental" in text
            and "insert into lake.assets.asset_incremental" in text
        ), "Completed incremental SQL must contain actual target DELETE and INSERT"
        assert not re.search(
            r"(?:drop table|create (?:or replace )?table) (?:if (?:not )?exists )?lake\.assets\.asset_incremental\b",
            text,
        ), "Completed incremental SQL must not drop or create the target table"
        command(
            "snapshot-second",
            ["snapshot", "--select", "asset_history"],
            "snapshot.t44a_probe.asset_history",
        )
        report["table_identities"]["snapshot_after"] = table_identity("asset_history")
        assert report["table_identities"]["snapshot_after"] == snap_identity, (
            "Snapshot history was replaced"
        )
        report["snapshot_after"] = history_rows(
            [(1, 100, True), (2, 200, False), (2, 210, True), (3, 50, True)]
        )
        target = command("docs", ["docs", "generate"])
        html = (target / "index.html").read_text()
        assert "window.__DBT_DOCS__" in html and list((target / "assets").glob("*.js"))
        models = list(target.glob("info_schema/v*/dbt.models.parquet"))
        assert len(models) == 1
        with duckdb.connect(config={"memory_limit": "256MB"}) as connection:
            assert connection.execute(
                "SELECT name FROM read_parquet(?) WHERE unique_id=?",
                [str(models[0]), "model.t44a_probe.asset_incremental"],
            ).fetchall() == [("asset_incremental",)]
        report["docs_files"] = {
            str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in target.rglob("*")
            if p.is_file()
        }
        (project / "models/python_asset.py").write_text(
            "def model(dbt, session):\n    dbt.config(materialized='table')\n    return dbt.ref('asset_incremental')\n"
        )
        _, _, result = command(
            "python",
            ["run", "--select", "python_asset"],
            "model.t44a_probe.python_asset",
            allow_failure=True,
        )
        if report["stages"]["python"]["exit"] != 0:
            assert result["status"] == "error" and re.search(
                r"Python models are not supported for duckdb adapter",
                result.get("message", ""),
                re.IGNORECASE,
            ), "Unrelated Python failure is not an allowed exception"
            report["python_model"] = {
                "status": "EXPLICIT_UNSUPPORTED_NATIVE_EXCEPTION",
                "message": result["message"],
            }
        else:
            assert result["status"] == "success"
            with connect() as connection:
                assert connection.execute(
                    "SELECT count(*),sum(amount) FROM lake.assets.python_asset"
                ).fetchone() == (3, 360)
            report["python_model"] = {"status": "SUPPORTED_NATIVE", "total": 360}
        with connect() as connection:
            assert connection.execute("SELECT sum(amount) FROM lake.sales.orders").fetchone() == (
                350,
            )
            connection.execute("LOAD postgres_scanner")
            connection.execute(
                f"ATTACH '{metadata.removeprefix('postgres:')}' AS metadata_pg (TYPE POSTGRES)"
            )
            assert connection.execute(
                "SELECT * FROM postgres_query('metadata_pg', $$SELECT value FROM ducklake_metadata WHERE key='version'$$)"
            ).fetchone() == ("1.1",)
            report["catalogSpec"] = "1.1"
        report["status"] = "PASS_REMAINING_DBT_ASSETS"
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        try:
            for source, _ in export_candidates(work):
                if not source.exists() or source.suffix != ".parquet":
                    continue
                assert source.stat().st_size <= 32 * MIB
                with duckdb.connect(config={"memory_limit": "256MB"}) as connection:
                    logical = json.dumps(
                        connection.execute(
                            "SELECT * FROM read_parquet(?)", [str(source)]
                        ).fetchall(),
                        default=str,
                    )
                assert not any(secret in logical for secret in secret_values), (
                    "Parquet logical secret exposure"
                )
                report["safe_parquet"][str(source.relative_to(work))] = hashlib.sha256(
                    source.read_bytes()
                ).hexdigest()
        except Exception as error:
            report["status"], report["artifact_validation_error"] = "FAIL", str(error)
        report["artifacts"] = {
            str(p.relative_to(stages)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in stages.rglob("*")
            if p.is_file()
        }
        text = json.dumps(report, indent=2) + "\n"
        assert len(text.encode()) <= 16 * MIB and not any(
            secret in text for secret in secret_values
        )
        (work / "dbt-assets.json").write_text(text)
    assert report["status"] == "PASS_REMAINING_DBT_ASSETS", "Assets evidence validation failed"
