"""tests/integration/t44a_qualification/compatibility_catalog.py

Actual PG DuckLake spec requests, distinct from physical DuckDB storage versions.
"""

import json
import os
from contextlib import closing
from pathlib import Path

from compatibility import ANSWER, PINS, connect, digest, execute, refused

DSN = "postgres:host=127.0.0.1 port=5432 dbname=probe user=postgres"
ACTOR = "t44a_producer_actor"
LEGACY = "n3_v10"
DATA = Path("/fixture/n3-spec10")


def prepare(stage, report):
    """Load only already frozen signed extensions into a bounded current engine."""
    os.environ["PGPASSFILE"] = "/secrets/pgpass-python"
    pins = json.loads(Path("/scripts/pins.json").read_text())["extensions"]
    con = connect(stage)
    try:
        for name in ["postgres_scanner", "ducklake"]:
            path = Path(
                "/home/probe/.duckdb/extensions/v2.0.0/linux_amd64", name + ".duckdb_extension"
            )
            assert path.is_file() and not path.is_symlink()
            assert digest(path.read_bytes()) == pins[name]["expanded_sha256"]
            execute(con, "LOAD " + name, report)
            row = execute(
                con,
                "SELECT loaded,installed,extension_version FROM duckdb_extensions() "
                f"WHERE extension_name='{name}'",
                report,
            ).fetchone()
            assert row == (True, True, pins[name]["revision"])
        execute(
            con,
            f"ATTACH '{DSN.removeprefix('postgres:')}' AS n3_pg (TYPE POSTGRES, READ_ONLY)",
            report,
        )
        return con
    except Exception:
        con.close()
        raise


def pg(con, sql, report):
    """Inspect native metadata without putting catalog passwords in SQL or argv."""
    return execute(con, "SELECT * FROM postgres_query('n3_pg', $$" + sql + "$$)", report).fetchall()


def attach(con, alias, schema, version, report, create=False):
    """Use explicit no-migration requests; only the owned spec1.0 fixture can be writable."""
    assert schema in {ACTOR, LEGACY} and alias in {"selected", "legacy_catalog", "mismatch"}
    options = f"METADATA_SCHEMA '{schema}', AUTOMATIC_MIGRATION FALSE"
    if version is not None:
        assert version in {"1.0", "1.1"}
        options += f", DUCKLAKE_VERSION '{version}'"
    if create:
        assert schema == LEGACY and version == "1.0"
        options += f", DATA_PATH '{DATA}', DATA_INLINING_ROW_LIMIT 0"
    else:
        options += ", READ_ONLY"
    execute(con, f"ATTACH 'ducklake:{DSN}' AS {alias} ({options})", report)


def state(con, schema, root, report):
    """Bind schema OID, table UUIDs, snapshots, spec and every native Parquet byte."""
    assert schema in {ACTOR, LEGACY} and root.resolve() == root
    metadata = dict(
        pg(con, f"SELECT key,value FROM {schema}.ducklake_metadata WHERE scope IS NULL", report)
    )
    assert Path(metadata["data_path"]) == root
    files = {}
    allocated = 0
    for path in [root, *root.rglob("*")]:
        assert not path.is_symlink()
        allocated += path.stat().st_blocks * 512
        if path.suffix == ".parquet":
            files[str(path.relative_to(root))] = digest(path.read_bytes())
    assert files and allocated <= PINS["clone_bound"]
    return {
        "namespace_oid": pg(
            con, f"SELECT oid::bigint FROM pg_namespace WHERE nspname='{schema}'", report
        )[0][0],
        "metadata_schema": schema,
        "root": str(root),
        "version": metadata["version"],
        "tables": pg(
            con,
            f"SELECT table_id,table_uuid::text FROM {schema}.ducklake_table ORDER BY table_id",
            report,
        ),
        "snapshots": pg(
            con, f"SELECT snapshot_id FROM {schema}.ducklake_snapshot ORDER BY snapshot_id", report
        ),
        "files": files,
    }


def selected(con, report, version="1.1"):
    """Refuse stale or misbound selected inputs before analytical reads."""
    captured = json.loads(Path("/work/producer-capture.json").read_text())
    assert captured["status"] == "PASS_SOURCE_PINNED_DLT_DUCKLAKE_CAPTURE"
    assert captured["source_commit"] == PINS["source_dlt_commit"]
    assert captured["captured_before"] == captured["captured_after"]
    expected = captured["captured_after"]["actor"]
    actual = state(con, ACTOR, Path("/fixture/data/producer-actor"), report)
    assert actual["version"] == "1.1"
    assert all(
        json.loads(json.dumps(value)) == expected[key]
        for key, value in actual.items()
        if key != "version"
    )
    attach(con, "selected", ACTOR, version, report)
    rows = execute(
        con, "SELECT id,order_date::VARCHAR,amount FROM selected.sales.orders ORDER BY id", report
    ).fetchall()
    assert json.loads(json.dumps(rows)) == expected["rows"] and expected["total"] == 350
    return actual


def run(stage, report):
    """Exercise explicit refusal and normal compatible recovery without catalog editing."""
    import duckdb

    assert stage in {
        "catalog-control11",
        "catalog-down",
        "catalog-recover11",
        "catalog-create10",
        "catalog-up",
        "catalog-default10",
        "catalog-explicit10",
        "catalog-default11",
    }
    with closing(prepare(stage, report)) as con:
        if stage in {"catalog-down", "catalog-up"}:
            schema, version, key = (
                (ACTOR, "1.0", "downgrade_error")
                if stage == "catalog-down"
                else (LEGACY, "1.1", "migration_error")
            )
            before = state(
                con,
                schema,
                Path("/fixture/data/producer-actor") if schema == ACTOR else DATA,
                report,
            )
            try:
                attach(con, "mismatch", schema, version, report)
            except duckdb.InvalidInputException as error:
                report["refusal"] = refused(
                    error, duckdb.InvalidInputException, PINS[key], "DuckLake catalog version"
                )
            else:
                raise AssertionError("Incompatible catalog request unexpectedly attached")
            report.update(before=before, after=state(con, schema, Path(before["root"]), report))
            assert report["before"] == report["after"]
            return
        actor = selected(con, report, None if stage == "catalog-default11" else "1.1")
        report["selected"] = actor
        if stage == "catalog-create10":
            assert not pg(con, f"SELECT oid FROM pg_namespace WHERE nspname='{LEGACY}'", report)
            assert not DATA.exists()
            DATA.mkdir(mode=0o700)
            try:
                attach(con, "legacy_catalog", LEGACY, "1.0", report, True)
                execute(con, "CREATE SCHEMA legacy_catalog.sales", report)
                execute(
                    con,
                    "CREATE TABLE legacy_catalog.sales.orders AS SELECT * FROM selected.sales.orders",
                    report,
                )
            finally:
                # Retain exact ownership even if native initialization fails partway through.
                found = pg(
                    con, f"SELECT oid::bigint FROM pg_namespace WHERE nspname='{LEGACY}'", report
                )
                if found:
                    report["owned_schema"] = {
                        "name": LEGACY,
                        "oid": found[0][0],
                        "absent_before": True,
                    }
            actual = state(con, LEGACY, DATA, report)
            assert actual["version"] == "1.0" and actual["namespace_oid"] != actor["namespace_oid"]
            assert {v[1] for v in actual["tables"]}.isdisjoint(v[1] for v in actor["tables"])
            report["legacy"] = actual
            assert state(con, ACTOR, Path(actor["root"]), report) == actor
        elif stage in {"catalog-default10", "catalog-explicit10"}:
            expected = json.loads(Path("/work/n3-catalog-create10.json").read_text())["legacy"]
            actual = state(con, LEGACY, DATA, report)
            assert json.loads(json.dumps(actual)) == expected
            attach(
                con,
                "legacy_catalog",
                LEGACY,
                None if stage == "catalog-default10" else "1.0",
                report,
            )
            report["actual"] = execute(
                con,
                "SELECT count(*),sum(amount),count(DISTINCT id) FROM legacy_catalog.sales.orders",
                report,
            ).fetchall()
            assert report["actual"] == [tuple(ANSWER[0])]
            report["legacy"] = state(con, LEGACY, DATA, report)
            assert json.loads(json.dumps(report["legacy"])) == expected
        else:
            assert stage in {"catalog-control11", "catalog-recover11", "catalog-default11"}
            report["actual"] = execute(
                con,
                "SELECT count(*),sum(amount),count(DISTINCT id) FROM selected.sales.orders",
                report,
            ).fetchall()
            assert report["actual"] == [tuple(ANSWER[0])]
