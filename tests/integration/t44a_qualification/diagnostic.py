"""tests/integration/t44a_qualification/diagnostic.py

Real OSS dbt materialization through the public signed DuckDB ADBC driver.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import tomllib


def main():
    """Initialize spec1.1, invoke ordinary dbt seed/build and verify authentic outputs."""
    import duckdb

    pins = json.loads(Path("/scripts/pins.json").read_text())
    os.environ.update(ADBC_DRIVER_PATH="/home/probe/adbc", PGPASSFILE="/secrets/pgpass-python")
    subprocess.run(["/tools/dbc-tool/dbc", "--help"], check=True)
    subprocess.run(
        [
            "/tools/dbc-tool/dbc",
            "install",
            "/tools/artifacts/duckdb_linux_amd64_v2.0.0.tar.gz",
            "--json",
        ],
        check=True,
    )
    manifests = list(Path(os.environ["ADBC_DRIVER_PATH"]).glob("*.toml"))
    assert len(manifests) == 1, "Expected the normal installed DuckDB manifest"
    manifest = tomllib.loads(manifests[0].read_text())
    driver = next(a for a in pins["artifacts"] if a["kind"] == "driver")
    libraries = list(Path(os.environ["ADBC_DRIVER_PATH"]).rglob("libduckdb.so"))
    assert len(libraries) == 1
    assert (
        hashlib.sha256(libraries[0].read_bytes()).hexdigest()
        == driver["member_hashes"]["libduckdb.so"]
    )
    assert "2.0.0" in str(manifest), "Installed driver version not bound"
    assert Path(manifest["Driver"]["shared"]["linux_amd64"]).resolve() == libraries[0].resolve(), (
        "Installed manifest driver path mismatch"
    )
    assert duckdb.__version__ == "2.0.0"
    metadata = "postgres:host=127.0.0.1 port=5432 dbname=probe user=postgres"
    with duckdb.connect(config={"memory_limit": "512MB", "threads": "2"}) as connection:
        connection.execute("LOAD postgres_scanner")
        connection.execute("LOAD ducklake")
        connection.execute(
            f"ATTACH 'ducklake:{metadata}' AS lake (DATA_PATH '/fixture/data', "
            "DUCKLAKE_VERSION '1.1', AUTOMATIC_MIGRATION FALSE, DATA_INLINING_ROW_LIMIT 0)"
        )
        assert connection.execute("SELECT version()").fetchone()[0].startswith("v2.")
        connection.execute("DETACH lake")
    project = Path("/work/project")
    for folder in ["models", "seeds", "tests"]:
        (project / folder).mkdir(parents=True, exist_ok=True)
    (project / "dbt_project.yml").write_text(
        "name: t44a_probe\nversion: '1.0'\nconfig-version: 2\nprofile: probe\n"
        "models:\n  t44a_probe:\n    +materialized: table\n    +catalog_name: lake\n    +database: lake\n"
        "seeds:\n  t44a_probe:\n    +catalog_name: lake\n    +database: lake\n"
    )
    (project / "profiles.yml").write_text(
        "probe:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        "      path: ':memory:'\n      database: memory\n      schema: sales\n      threads: 2\n"
        "      extensions: [postgres_scanner, ducklake]\n      settings:\n"
        "        memory_limit: 512MB\n        max_temp_directory_size: 512MB\n"
        "        temp_directory: /work/duckdb-temp\n"
    )
    (project / "catalogs.yml").write_text(
        "catalogs:\n  - name: lake\n    type: ducklake\n    table_format: default\n"
        "    config:\n      duckdb:\n"
        f"        metadata_path: '{metadata}'\n        data_path: /fixture/data\n"
        "        catalog_database: lake\n        automatic_migration: false\n"
        "        data_inlining_row_limit: 0\n"
    )
    (project / "seeds/input.csv").write_text(
        "id,order_date,amount\n1,2026-09-01,100\n2,2026-09-15,200\n3,2026-10-01,50\n"
    )
    (project / "models/orders.sql").write_text("select * from {{ ref('input') }}\n")
    # Real dbt parses this YAML into authentic manifest/catalog metadata for the app.
    (project / "models/schema.yml").write_text(
        "version: 2\nmodels:\n  - name: orders\n    columns:\n"
        "      - name: id\n        data_type: INTEGER\n"
        "      - name: order_date\n        data_type: DATE\n"
        "        config:\n          meta:\n            dimension:\n"
        "              type: date\n              time_intervals: [RAW, DAY]\n"
        "      - name: amount\n        data_type: INTEGER\n"
        "        config:\n          meta:\n            metrics:\n"
        "              total_amount:\n                type: sum\n"
    )
    (project / "tests/engine_and_total.sql").write_text(
        "select 1 as failure where version() not like 'v2.%' or "
        "(select sum(amount) from {{ ref('orders') }}) <> 350\n"
    )
    common = [
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(project),
        "--no-version-check",
        "--no-send-anonymous-usage-stats",
    ]
    version = subprocess.run(
        ["/work/venv/bin/dbt", "--version"], capture_output=True, text=True, check=True
    )
    print(version.stdout + version.stderr)
    assert re.search(r"^dbt-oss 2\.0\.8(?:\s|$)", version.stdout + version.stderr, re.MULTILINE), (
        "Unexpected actual OSS CLI version"
    )
    for command in [["build", "--help"], ["seed", *common], ["build", *common, "--write-catalog"]]:
        subprocess.run(["/work/venv/bin/dbt", *command], check=True)
    hashes = {}
    for name in ["manifest.json", "run_results.json", "catalog.json"]:
        data = (project / "target" / name).read_bytes()
        value = json.loads(data)
        assert value, "Missing authentic dbt artifact"
        hashes[name] = hashlib.sha256(data).hexdigest()
    result = json.loads((project / "target/run_results.json").read_text())
    assert len(result["results"]) >= 3
    assert all(row["status"] in {"success", "pass"} for row in result["results"])
    assert any(row["unique_id"].startswith("test.") for row in result["results"])
    with duckdb.connect(config={"memory_limit": "256MB", "threads": "2"}) as pg:
        pg.execute("LOAD postgres_scanner")
        pg.execute(f"ATTACH '{metadata.removeprefix('postgres:')}' AS metadata_pg (TYPE POSTGRES)")
        assert pg.execute(
            "SELECT * FROM postgres_query('metadata_pg', "
            "$$SELECT value FROM ducklake_metadata WHERE key='version'$$)"
        ).fetchone() == ("1.1",)
    with duckdb.connect(config={"memory_limit": "512MB"}) as connection:
        connection.execute(
            f"ATTACH 'ducklake:{metadata}' AS lake (READ_ONLY, AUTOMATIC_MIGRATION FALSE)"
        )
        assert connection.execute("SELECT sum(amount) FROM lake.sales.orders").fetchone() == (350,)
        assert connection.execute(
            "SELECT sum(amount) FROM lake.sales.orders WHERE order_date < DATE '2026-10-01'"
        ).fetchone() == (300,)
    assert list(Path("/fixture/data").rglob("*.parquet")), "Expected physical DuckLake data"
    if "--view-gate" in sys.argv[1:]:
        from dbt_view import run_view

        run_view(project, metadata)
    Path("/work/diagnostic.json").write_text(
        json.dumps(
            {
                "status": "PASS_DBT_DRIVER_DIAGNOSTIC",
                "catalogSpec": "1.1",
                "total": 350,
                "september": 300,
                "dbtArtifacts": hashes,
                "driverManifest": manifest,
                "proof": "Actual dbt seed/table/test/catalog; Python readback independent, not full Lightdash app proof",
            },
            indent=2,
        )
    )
    print("PASS_DBT_DRIVER_DIAGNOSTIC")


if __name__ == "__main__":
    main()
