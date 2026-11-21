"""tests/integration/t44a_qualification/producer_capture.py

Real source-pinned dlt DuckLake loads and selected native captured inputs.
"""

import hashlib
import json
import os
from pathlib import Path

from dlt_source import PINS, verify_install
from native_namespaces import bound_file

MIB = 1024**2
BINDINGS = {
    name: ("t44a_producer_" + name, Path("/fixture/data/producer-" + name))
    for name in ["raw", "capture", "actor"]
}


def run_capture():
    """Stop on the first real load failure; never substitute another producer."""
    os.environ.update(
        PGPASSFILE="/secrets/pgpass-python",
        DLT_DATA_DIR="/work/dlt-data",
        DLT_PROJECT_DIR="/work/dlt-project",
        RUNTIME__DLTHUB_TELEMETRY="false",
        NORMALIZE__WORKERS="1",
        LOAD__WORKERS="1",
    )
    for directory in ["/work/dlt-data", "/work/dlt-project"]:
        Path(directory).mkdir(exist_ok=True)
    import dlt
    import duckdb
    from dlt.common import json as dlt_json
    from dlt.destinations.impl.ducklake.configuration import DuckLakeCredentials

    assert duckdb.__version__ == "2.0.0"
    report = {"status": "FAIL", "stages": {}, "sql": [], "source_commit": PINS["source_commit"]}
    known = list(json.loads(Path("/secrets/catalog.json").read_text()).values())
    metadata = "postgres:host=127.0.0.1 port=5432 dbname=probe user=postgres"

    def save_bytes(name, data, bound=2 * MIB):
        assert len(data) <= bound, "Producer receipt bound exceeded"
        assert not any(value.encode() in data for value in known), (
            "Producer receipt secret exposure"
        )
        Path("/work", name).write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def execute(connection, sql):
        row = {"sql": sql}
        report["sql"].append(row)
        try:
            value = connection.execute(sql)
            row["status"] = "executed"
            return value
        except duckdb.Error as error:
            row.update(status="native_error", type=type(error).__name__, message=str(error))
            raise

    def attach(connection, name, readonly=False):
        schema, root = BINDINGS[name]
        options = (
            f"METADATA_SCHEMA '{schema}', DATA_PATH '{root}', DUCKLAKE_VERSION '1.1', "
            "AUTOMATIC_MIGRATION FALSE, DATA_INLINING_ROW_LIMIT 0"
        )
        if readonly:
            options += ", READ_ONLY"
        execute(connection, f"ATTACH 'ducklake:{metadata}' AS {name} ({options})")

    def connect():
        return duckdb.connect(config={"memory_limit": "512MB", "threads": "2"})

    def pg(query):
        with connect() as connection:
            connection.execute("LOAD postgres_scanner")
            connection.execute(
                f"ATTACH '{metadata.removeprefix('postgres:')}' AS pg (TYPE POSTGRES)"
            )
            return connection.execute(
                "SELECT * FROM postgres_query('pg', $$" + query + "$$)"
            ).fetchall()

    def observe(name, total):
        schema, root = BINDINGS[name]
        with connect() as connection:
            attach(connection, name, True)
            rows = execute(
                connection, f"SELECT id,order_date,amount FROM {name}.sales.orders ORDER BY id"
            ).fetchall()
        expected = [(1, "2026-09-01", 100), (2, "2026-09-15", total - 150), (3, "2026-10-01", 50)]
        actual = [(r[0], r[1].isoformat(), r[2]) for r in rows]
        assert actual == expected and len({r[0] for r in rows}) == 3, (
            "Native producer/capture rows differ"
        )
        stored = pg(f"SELECT key,value FROM {schema}.ducklake_metadata WHERE scope IS NULL")
        assert dict(stored)["version"] == "1.1" and Path(dict(stored)["data_path"]) == root
        files = {}
        for kind in ["data", "delete"]:
            for sp, sr, tp, tr, fp, fr, size, end in pg(
                f"SELECT s.path,s.path_is_relative,t.path,t.path_is_relative,"
                f"f.path,f.path_is_relative,f.file_size_bytes,f.end_snapshot "
                f"FROM {schema}.ducklake_{kind}_file f "
                f"JOIN {schema}.ducklake_table t USING(table_id) "
                f"JOIN {schema}.ducklake_schema s USING(schema_id)"
            ):
                path = bound_file(root, [(sp, sr), (tp, tr), (fp, fr)])
                if not path.exists():
                    assert end is not None, "Missing current native data file"
                    continue
                assert path.is_file() and not path.is_symlink() and path.stat().st_size == size
                files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        physical = {str(path.relative_to(root)) for path in root.rglob("*.parquet")}
        assert physical == set(files) and files, "Missing/unbound native producer/capture Parquet"
        return {
            "rows": actual,
            "total": sum(r[2] for r in rows),
            "namespace_oid": pg(f"SELECT oid::bigint FROM pg_namespace WHERE nspname='{schema}'")[
                0
            ][0],
            "metadata_schema": schema,
            "root": str(root),
            "tables": pg(
                f"SELECT table_id,table_uuid::text FROM {schema}.ducklake_table ORDER BY table_id"
            ),
            "snapshots": pg(
                f"SELECT snapshot_id FROM {schema}.ducklake_snapshot ORDER BY snapshot_id"
            ),
            "files": files,
        }

    def check_fit():
        paths = [root for _, root in BINDINGS.values()] + [Path("/work/dlt-data")]
        allocated = sum(
            p.stat().st_blocks * 512
            for root in paths
            for p in [root, *root.rglob("*")]
            if p.exists()
        )
        allocated += pg(
            "SELECT coalesce(sum(pg_total_relation_size(c.oid)),0)::bigint FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname LIKE 't44a_producer_%'"
        )[0][0]
        assert allocated <= 256 * MIB, "Producer fixture/PG/state allocation exceeds reserved bound"
        report["fixture_allocated_bytes"] = allocated

    try:
        report["installed"] = verify_install()
        for name, (_, root) in BINDINGS.items():
            assert not root.exists() and root.parent.resolve() == Path("/fixture/data")
            root.mkdir(mode=0o755)
            with connect() as connection:
                attach(connection, name)
        source = Path("/work/producer-orders.json")
        data = [
            {"id": 1, "order_date": "2026-09-01", "amount": 100},
            {"id": 2, "order_date": "2026-09-15", "amount": 200},
            {"id": 3, "order_date": "2026-10-01", "amount": 50},
        ]
        schema, root = BINDINGS["raw"]
        credentials = DuckLakeCredentials(
            ducklake_name="raw",
            metadata_schema=schema,
            catalog="postgresql://postgres@127.0.0.1:5432/probe",
            storage=str(root),
        )
        credentials.extensions = ["postgres_scanner", "ducklake"]
        credentials.global_config = {
            "memory_limit": "512MB",
            "threads": "2",
            "max_temp_directory_size": "512MB",
            "temp_directory": "/work/duckdb-temp",
        }
        # This is the pinned destination's supported per-connection SQL configuration.
        # Its normal ATTACH IF NOT EXISTS then uses this same binding, without migration.
        credentials.statements = [
            f"ATTACH 'ducklake:{metadata}' AS raw (METADATA_SCHEMA '{schema}', "
            f"DATA_PATH '{root}', DUCKLAKE_VERSION '1.1', AUTOMATIC_MIGRATION FALSE, "
            "DATA_INLINING_ROW_LIMIT 0)"
        ]
        pipeline = dlt.pipeline(
            pipeline_name="t44a_producer",
            pipelines_dir="/work/dlt-data",
            destination=dlt.destinations.ducklake(
                credentials=credentials, automatic_migration=False
            ),
            dataset_name="sales",
        )

        @dlt.resource(
            name="orders",
            primary_key="id",
            write_disposition="merge",
            columns={
                "id": {"data_type": "bigint"},
                "order_date": {"data_type": "date"},
                "amount": {"data_type": "bigint"},
            },
        )
        def orders():
            raw = source.read_bytes()
            dlt.current.resource_state()["source_revision"] = hashlib.sha256(raw).hexdigest()
            yield json.loads(raw)

        def load(stage):
            source.write_text(json.dumps(data, sort_keys=True) + "\n")
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            info = pipeline.run(
                orders, write_disposition={"disposition": "merge", "strategy": "delete-insert"}
            )
            info.raise_on_failed_jobs()
            assert len(info.loads_ids) == 1 and all(p.state == "loaded" for p in info.load_packages)
            state = pipeline.state
            assert source_hash in json.dumps(state["sources"], default=str)
            report["stages"][stage] = {
                "source_rows": [dict(r) for r in data],
                "source_sha256": source_hash,
                "load_ids": info.loads_ids,
                "state_version": state["_state_version"],
                "state_version_hash": state["_version_hash"],
                "load_info_sha256": save_bytes(
                    "producer-" + stage + "-load.json", dlt_json.dumpb(info.asdict())
                ),
                "state_sha256": save_bytes(
                    "producer-" + stage + "-state.json",
                    (Path(pipeline.working_dir) / "state.json").read_bytes(),
                ),
            }
            with connect() as connection:
                attach(connection, "raw", True)
                loads = execute(
                    connection, "SELECT load_id,status FROM raw.sales._dlt_loads ORDER BY load_id"
                ).fetchall()
                states = execute(
                    connection,
                    "SELECT version,engine_version,pipeline_name,version_hash,_dlt_load_id FROM raw.sales._dlt_pipeline_state ORDER BY version",
                ).fetchall()
            assert (info.loads_ids[0], 0) in loads
            assert any(r[3] == state["_version_hash"] and r[4] == info.loads_ids[0] for r in states)
            report["stages"][stage].update(native_loads=loads, native_checkpoints=states)
            check_fit()

        load("first")
        report["raw_first"] = observe("raw", 350)
        for target, origin in [("capture", "raw"), ("actor", "capture")]:
            with connect() as connection:
                attach(connection, origin, True)
                attach(connection, target)
                execute(connection, "BEGIN")
                execute(connection, f"CREATE SCHEMA {target}.sales")
                execute(
                    connection,
                    f"CREATE TABLE {target}.sales.orders AS SELECT id,order_date,amount FROM {origin}.sales.orders",
                )
                execute(connection, "COMMIT")
        before = {name: observe(name, 350) for name in ["capture", "actor"]}
        all_states = [report["raw_first"], *before.values()]
        assert len({r["namespace_oid"] for r in all_states}) == 3
        table_ids = [{r[1] for r in state["tables"]} for state in all_states]
        assert all(ids for ids in table_ids) and all(
            table_ids[a].isdisjoint(table_ids[b]) for a in range(3) for b in range(a)
        )
        report["captured_before"] = before
        data[1]["amount"] = 210
        load("second")
        report["raw_second"] = observe("raw", 360)
        assert (
            report["stages"]["first"]["source_sha256"]
            != report["stages"]["second"]["source_sha256"]
        )
        assert (
            report["stages"]["second"]["state_version"] > report["stages"]["first"]["state_version"]
        )
        after = {name: observe(name, 350) for name in ["capture", "actor"]}
        assert before == after, (
            "Original captured/selected actor inputs changed after source advance"
        )
        assert report["raw_first"]["snapshots"] != report["raw_second"]["snapshots"]
        report["captured_after"] = after
        check_fit()
        report["status"] = "PASS_SOURCE_PINNED_DLT_DUCKLAKE_CAPTURE"
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        save_bytes(
            "producer-capture.json",
            json.dumps(report, indent=2, default=str).encode() + b"\n",
            8 * MIB,
        )


if __name__ == "__main__":
    run_capture()
