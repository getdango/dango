"""tests/integration/t44a_qualification/dbt_view.py

Early real OSS dbt view gate, separate from the proven app table fixture.
"""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def run_view(table_project, metadata):
    """Build a genuine persisted lake view and independently read it after dbt exits."""
    import duckdb

    project = Path("/work/dbt-view-project")
    assert not project.exists(), "View project must be fresh"
    (project / "models").mkdir(parents=True)
    for name in ["dbt_project.yml", "profiles.yml", "catalogs.yml"]:
        shutil.copyfile(table_project / name, project / name)
    (project / "models/orders_view.sql").write_text(
        "{{ config(materialized='view') }}\nselect * from lake.sales.orders\n"
    )
    command = [
        "/work/venv/bin/dbt",
        "build",
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(project),
        "--no-version-check",
        "--no-send-anonymous-usage-stats",
        "--select",
        "orders_view",
        "--write-catalog",
    ]
    report = {"status": "FAIL", "stage": "dbt-build", "command": command}
    try:
        completed = subprocess.run(command, check=False)
        report["dbt_exit_code"] = completed.returncode
        assert completed.returncode == 0, "Real dbt view build failed; authentic outputs retained"
        report["stage"] = "authentic-artifacts"
        manifest = json.loads((project / "target/manifest.json").read_text())
        results = json.loads((project / "target/run_results.json").read_text())
        catalog = json.loads((project / "target/catalog.json").read_text())
        uid = "model.t44a_probe.orders_view"
        node = manifest["nodes"][uid]
        assert node["config"]["materialized"] == "view"
        assert (node["database"], node["schema"], node["alias"]) == ("lake", "sales", "orders_view")
        assert [(row["unique_id"], row["status"]) for row in results["results"]] == [
            (uid, "success")
        ]
        assert catalog["nodes"][uid]["metadata"]["type"].upper() == "VIEW"
        report["stage"] = "independent-python-readback"
        with duckdb.connect(config={"memory_limit": "512MB", "threads": "2"}) as connection:
            connection.execute(
                f"ATTACH 'ducklake:{metadata}' AS lake (READ_ONLY, AUTOMATIC_MIGRATION FALSE)"
            )
            assert connection.execute(
                "SELECT table_type FROM information_schema.tables "
                "WHERE table_catalog='lake' AND table_schema='sales' AND table_name='orders_view'"
            ).fetchall() == [("VIEW",)]
            assert connection.execute(
                "SELECT sum(amount) FROM lake.sales.orders_view"
            ).fetchone() == (350,)
            assert connection.execute(
                "SELECT sum(amount) FROM lake.sales.orders_view "
                "WHERE order_date >= DATE '2026-09-01' AND order_date < DATE '2026-10-01'"
            ).fetchone() == (300,)
            connection.execute("LOAD postgres_scanner")
            connection.execute(
                f"ATTACH '{metadata.removeprefix('postgres:')}' AS metadata_pg (TYPE POSTGRES)"
            )
            assert connection.execute(
                "SELECT * FROM postgres_query('metadata_pg', "
                "$$SELECT value FROM ducklake_metadata WHERE key='version'$$)"
            ).fetchone() == ("1.1",)
        report.update(
            status="PASS_REAL_DBT_VIEW",
            relation="lake.sales.orders_view",
            catalogSpec="1.1",
            total=350,
            september=300,
        )
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        report["artifacts"] = {
            name: hashlib.sha256((project / "target" / name).read_bytes()).hexdigest()
            for name in ["manifest.json", "run_results.json", "catalog.json"]
            if (project / "target" / name).exists()
        }
        report["inputs"] = {
            str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                project / name
                for name in [
                    "dbt_project.yml",
                    "profiles.yml",
                    "catalogs.yml",
                    "models/orders_view.sql",
                ]
            ]
        }
        text = json.dumps(report, indent=2) + "\n"
        assert len(text.encode()) <= 32 * 1024**2, "View report bound exceeded"
        Path("/work/dbt-view.json").write_text(text)
