"""tests/integration/t44a_qualification/restore_native.py

Read-only native replacement identities inside the existing one-CPU actor budget.
"""

import hashlib
import json
import os
import zipfile
from pathlib import Path

DSN = "host=127.0.0.1 port=5432 dbname=probe user=postgres"


def digest(data):
    """Hash only exact bytes."""
    return hashlib.sha256(data).hexdigest()


def main():
    """Prove coherent350 and authentic captured actor data, without catalog migration."""
    import _duckdb
    import duckdb

    assert duckdb.__version__ == "2.0.0"
    pins = json.loads(Path("/scripts/pins.json").read_text())
    wheel = next(p for p in pins["artifacts"] if p["file"].startswith("duckdb-2.0.0-"))
    archive = Path("/tools/artifacts") / wheel["file"]
    with archive.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == wheel["sha256"]
    with zipfile.ZipFile(archive) as source:
        with source.open("_duckdb.abi3.so") as stream:
            native_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    with Path(_duckdb.__file__).open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == native_sha
    spill = Path("/work/restore-spill")
    spill.mkdir(mode=0o700, exist_ok=True)
    assert spill.resolve() == spill and not spill.is_symlink()
    statements = []
    captured = json.loads(Path("/work/producer-capture.json").read_text())
    assert captured["source_commit"] == "d220927d13a79740ac799419d08ed9847f7b5069"
    assert captured["captured_before"] == captured["captured_after"]
    os.environ["PGPASSFILE"] = "/secrets/pgpass-python"
    con = duckdb.connect(
        config={
            "memory_limit": "128MB",
            "temp_directory": str(spill),
            "allow_unsigned_extensions": "false",
            "threads": "1",
            "max_temp_directory_size": "0B",
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
        }
    )

    def execute(sql, params=None):
        """Retain actual native SQL while passing it unchanged to the frozen engine."""
        statements.append(sql)
        return con.execute(sql, params)

    try:
        assert (
            execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0] == "0 bytes"
        )
        assert execute(
            "SELECT current_setting('temp_directory'),current_setting('threads'),current_setting('allow_unsigned_extensions')"
        ).fetchone() == (str(spill), 1, False)
        extensions = {}
        for name in ["postgres_scanner", "ducklake"]:
            body = Path(
                "/home/probe/.duckdb/extensions/v2.0.0/linux_amd64", name + ".duckdb_extension"
            ).read_bytes()
            assert digest(body) == pins["extensions"][name]["expanded_sha256"]
            execute("LOAD " + name)
            row = execute(
                "SELECT loaded,installed,extension_version FROM duckdb_extensions() WHERE extension_name=?",
                [name],
            ).fetchone()
            assert row == (True, True, pins["extensions"][name]["revision"])
            extensions[name] = {"sha256": digest(body), "revision": row[2]}
        execute(f"ATTACH '{DSN}' AS pg (TYPE POSTGRES, READ_ONLY)")

        def pg(sql):
            return execute("SELECT * FROM postgres_query('pg', $$" + sql + "$$)").fetchall()

        catalogs = {}
        for label in ["capture", "actor"]:
            schema = "t44a_producer_" + label
            root = Path("/fixture/data/producer-" + label)
            expected = captured["captured_after"][label]
            metadata = dict(
                pg(f"SELECT key,value FROM {schema}.ducklake_metadata WHERE scope IS NULL")
            )
            state = {
                "namespace_oid": pg(
                    f"SELECT oid::bigint FROM pg_namespace WHERE nspname='{schema}'"
                )[0][0],
                "metadata_schema": schema,
                "root": str(root),
                "tables": pg(
                    f"SELECT table_id,table_uuid::text FROM {schema}.ducklake_table ORDER BY table_id"
                ),
                "snapshots": pg(
                    f"SELECT snapshot_id FROM {schema}.ducklake_snapshot ORDER BY snapshot_id"
                ),
                "files": {
                    str(p.relative_to(root)): digest(p.read_bytes())
                    for p in root.rglob("*.parquet")
                },
            }
            assert metadata["version"] == "1.1" and metadata["data_path"] == str(root) + "/", (
                "Restore metadata version/data-root identity mismatch"
            )
            assert state["files"] and all(
                json.loads(json.dumps(v)) == expected[k] for k, v in state.items()
            )
            execute(
                f"ATTACH 'ducklake:postgres:{DSN}' AS {label} (METADATA_SCHEMA '{schema}', READ_ONLY, AUTOMATIC_MIGRATION FALSE, DUCKLAKE_VERSION '1.1')"
            )
            rows = execute(
                f"SELECT id,order_date::VARCHAR,amount FROM {label}.sales.orders ORDER BY id"
            ).fetchall()
            assert (
                json.loads(json.dumps(rows)) == expected["rows"] and sum(r[2] for r in rows) == 350
            )
            catalogs[label] = state
        execute(
            f"ATTACH 'ducklake:postgres:{DSN}' AS lake (DATA_PATH '/work/held-data', READ_ONLY, AUTOMATIC_MIGRATION FALSE, DUCKLAKE_VERSION '1.1')"
        )
        coherent = [
            execute(sql).fetchone()[0]
            for sql in [
                "SELECT sum(amount) FROM lake.sales.orders",
                "SELECT amount FROM lake.finance.orders",
                "SELECT amount FROM lake.sales.summary",
                "SELECT revision FROM lake.main.release_marker",
            ]
        ]
        assert coherent == [350, 7, 350, "r0"]
        snapshot = execute("SELECT id FROM ducklake_current_snapshot('lake')").fetchone()[0]
        result = {
            "status": "PASS_READ_ONLY_RESTORE_NATIVE_350",
            "coherent": coherent,
            "snapshot": snapshot,
            "catalogs": catalogs,
            "extensions": extensions,
            "source_commit": captured["source_commit"],
            "identity": {
                "version": duckdb.__version__,
                "native_sha256": native_sha,
                "wheel_sha256": wheel["sha256"],
            },
            "sql": statements,
        }
        text = json.dumps(result, sort_keys=True)
        assert len(text) <= 2 * 1024**2
        assert not any(
            v in text for v in json.loads(Path("/secrets/app.json").read_text()).values()
        )
        print(text)
    finally:
        con.close()


if __name__ == "__main__":
    main()
