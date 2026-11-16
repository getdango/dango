"""tests/integration/t44a_qualification/notebook_fixture.py

Exact captured-input binding and native notebook branch; no Dango protocol claim.
"""

import json
import os
import re
import tempfile
import time
from pathlib import Path

from held_reader import DATA, METADATA, captures, digest, state

MIB = 1024**2
SPILL_ROOT = Path("/work/notebook-spill")
SQL = (
    "SELECT sum(amount),min(snapshot_id),max(snapshot_id),min(revision),max(revision) "
    "FROM lake.sales.notebook_orders"
)


def save(name, value):
    """Bound original receipts and reject known secrets before writing."""
    data = json.dumps(value, indent=2, default=str).encode() + b"\n"
    known = json.loads(Path("/secrets/catalog.json").read_text()).values()
    assert len(data) <= 2 * MIB and not any(v.encode() in data for v in known)
    Path("/work", name).write_bytes(data)


def verify_spill(actual, expected):
    """Refuse default/unowned directories or any effective nonzero spill allowance."""
    assert expected.parent == SPILL_ROOT and expected.is_dir() and not expected.is_symlink()
    assert SPILL_ROOT.is_dir() and not SPILL_ROOT.is_symlink()
    assert SPILL_ROOT.resolve() == SPILL_ROOT and expected.resolve() == expected
    assert expected.stat().st_uid == SPILL_ROOT.stat().st_uid == os.getuid()
    assert expected.stat().st_mode & 0o777 == SPILL_ROOT.stat().st_mode & 0o777 == 0o700
    directory, cap = actual
    assert directory == str(expected), "Notebook spill directory differs/defaulted"
    assert isinstance(cap, str) and re.fullmatch(r"0(?:\.0+)?\s*(?:bytes|B|KiB|MiB|GiB)", cap), (
        "Notebook effective spill must be disabled"
    )


def engine_connection():
    """Own and measure every connection's spill directory; tiny fixture spill is disabled."""
    import duckdb

    assert duckdb.__version__ == "2.0.0"
    SPILL_ROOT.mkdir(mode=0o700, exist_ok=True)
    assert SPILL_ROOT.is_dir() and not SPILL_ROOT.is_symlink()
    assert SPILL_ROOT.resolve() == SPILL_ROOT and SPILL_ROOT.stat().st_uid == os.getuid()
    directory = Path(tempfile.mkdtemp(prefix="connection-", dir=SPILL_ROOT))
    con = duckdb.connect(
        config={
            "memory_limit": "128MB",
            "threads": "1",
            "temp_directory": str(directory),
            "max_temp_directory_size": "0B",
        }
    )
    try:
        actual = con.execute(
            "SELECT current_setting('temp_directory'),current_setting('max_temp_directory_size')"
        ).fetchone()
        verify_spill(actual, directory)
        path = Path("/work/notebook-connections.jsonl")
        assert not path.exists() or path.stat().st_size < MIB
        with path.open("a") as output:
            output.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "temp_directory": actual[0],
                        "max_temp_directory_size": actual[1],
                        "spill_cap_bytes": 0,
                    }
                )
                + "\n"
            )
        return con
    except Exception:
        con.close()
        raise


def connection():
    """Keep the accepted held helper untouched while bounding every notebook analytical client."""
    os.environ["PGPASSFILE"] = "/secrets/pgpass-python"
    con = engine_connection()
    try:
        con.execute("LOAD postgres_scanner")
        con.execute("LOAD ducklake")
        con.execute(
            f"ATTACH 'ducklake:{METADATA}' AS lake (DATA_PATH '{DATA}', AUTOMATIC_MIGRATION FALSE, DATA_INLINING_ROW_LIMIT 0)"
        )
        return con
    except Exception:
        con.close()
        raise


def pg_connection():
    """Use the frozen signed extension with the same measured zero-spill policy."""
    os.environ["PGPASSFILE"] = "/secrets/pgpass-python"
    con = engine_connection()
    try:
        con.execute("LOAD postgres_scanner")
        con.execute(f"ATTACH '{METADATA.removeprefix('postgres:')}' AS control_pg (TYPE POSTGRES)")
        return con
    except Exception:
        con.close()
        raise


def pg(con, query):
    """Run only qualification-owned SQL, without a catalog password in argv/SQL."""
    assert "$$" not in query
    return con.execute("SELECT * FROM postgres_query('control_pg', $$" + query + "$$)").fetchall()


def verify_capture(expected, selected_schema="t44a_producer_actor"):
    """Compare external producer identities before any analytical table read."""
    assert set(expected) == {"capture", "actor"}, "Missing external captured identities"
    with pg_connection() as con:
        for name in ["capture", "actor"]:
            item = expected[name]
            schema = "t44a_producer_" + name
            actual_schema = selected_schema if name == "actor" else schema
            assert actual_schema in {"t44a_producer_actor", "t44a_producer_raw", schema}
            oid = pg(con, f"SELECT oid::bigint FROM pg_namespace WHERE nspname='{actual_schema}'")
            assert oid == [(item["namespace_oid"],)], "REFUSE_CATALOG_IDENTITY"
            assert item["metadata_schema"] == schema
            root = Path("/fixture/data/producer-" + name)
            assert item["root"] == str(root) and root.is_dir() and not root.is_symlink()
            assert not any(p.is_symlink() for p in root.rglob("*"))
            stored = dict(
                pg(con, f"SELECT key,value FROM {schema}.ducklake_metadata WHERE scope IS NULL")
            )
            assert stored["version"] == "1.1" and Path(stored["data_path"]) == root
            tables = pg(
                con,
                f"SELECT table_id,table_uuid::text FROM {schema}.ducklake_table ORDER BY table_id",
            )
            snapshots = pg(
                con, f"SELECT snapshot_id FROM {schema}.ducklake_snapshot ORDER BY snapshot_id"
            )
            assert json.loads(json.dumps(tables)) == item["tables"], "REFUSE_TABLE_IDENTITY"
            assert json.loads(json.dumps(snapshots)) == item["snapshots"], "REFUSE_STALE_SNAPSHOT"
            files = {
                str(p.relative_to(root)): digest(p.read_bytes()) for p in root.rglob("*.parquet")
            }
            assert files == item["files"] and files, "REFUSE_CAPTURE_FILES"


def prepare(key, execution):
    """Initialize one accepted branch from authentic, externally recorded selected350."""
    from dlt_source import PINS

    raw = Path("/work/producer-capture.json").read_bytes()
    producer = json.loads(raw)
    assert producer["status"] == "PASS_SOURCE_PINNED_DLT_DUCKLAKE_CAPTURE"
    assert producer["source_commit"] == PINS["source_commit"]
    assert producer["captured_before"] == producer["captured_after"]
    expected = producer["captured_after"]
    verify_capture(expected)
    assert not DATA.exists(), "Working branch must be fresh"
    with connection() as con:
        con.execute(
            f"ATTACH 'ducklake:{METADATA}' AS selected (METADATA_SCHEMA 't44a_producer_actor', "
            "DATA_PATH '/fixture/data/producer-actor', READ_ONLY, AUTOMATIC_MIGRATION FALSE)"
        )
        assert con.execute("SELECT sum(amount) FROM selected.sales.orders").fetchone() == (350,)
        for query in [
            "BEGIN",
            "CREATE SCHEMA lake.sales",
            "CREATE SCHEMA lake.finance",
            "CREATE TABLE lake.sales.orders AS SELECT id,order_date,amount FROM selected.sales.orders",
            "CREATE TABLE lake.finance.orders AS SELECT 7::INTEGER amount",
            "CREATE TABLE lake.sales.summary AS SELECT 350::INTEGER amount",
            "CREATE TABLE lake.main.release_marker AS SELECT 'r0'::VARCHAR revision",
            "COMMIT",
        ]:
            con.execute(query)
        view = (
            "CREATE VIEW lake.sales.notebook_orders AS SELECT o.amount+g.zero AS amount,"
            "s.id AS snapshot_id,r.revision FROM lake.sales.orders o "
            "CROSS JOIN lake.main.release_marker r CROSS JOIN ducklake_current_snapshot('lake') s "
            "CROSS JOIN postgres_query('__ducklake_metadata_lake', "
            f"'SELECT public.t44a_held_gate({key}) AS zero') g"
        )
        con.execute(view)
        before = state(con)
        assert (before["total"], before["finance"], before["summary"], before["release"]) == (
            350,
            7,
            350,
            "r0",
        )
        original = captures(con)
        with pg_connection() as metadata:
            oid = pg(metadata, "SELECT oid::bigint FROM pg_namespace WHERE nspname='public'")[0][0]
            table_ids = pg(
                metadata,
                "SELECT table_id,table_uuid::text FROM public.ducklake_table ORDER BY table_id",
            )
            view_rows = pg(
                metadata,
                "SELECT view_id,view_uuid::text,sql FROM public.ducklake_view WHERE end_snapshot IS NULL ORDER BY view_id",
            )
    binding = {
        "execution_uuid": execution,
        "key": key,
        "producer_report_sha256": digest(raw),
        "source_commit": PINS["source_commit"],
        "source_archive_sha256": PINS["source"]["sha256"],
        "selected": expected,
        "captures": original,
        "before": before,
        "working": {
            "root": str(DATA),
            "metadata_schema": "public",
            "namespace_oid": oid,
            "tables": table_ids,
            "views": view_rows,
        },
        "sql": SQL,
        "sql_sha256": digest(SQL.encode()),
        "view_sql_sha256": digest(view.encode()),
        "notebook_sha256": digest(Path("/scripts/captured_notebook.py").read_bytes()),
    }
    save("notebook-binding.json", binding)
    return json.loads(json.dumps(binding))


def commit(binding):
    """Commit once while the independently observed notebook COPY is held."""
    with connection() as con:
        assert json.loads(json.dumps(state(con))) == binding["before"]
        assert captures(con) == binding["captures"]
        queries = [
            "BEGIN",
            "UPDATE lake.sales.orders SET amount=210 WHERE id=2",
            "UPDATE lake.finance.orders SET amount=8",
            "UPDATE lake.sales.summary SET amount=360",
            "UPDATE lake.main.release_marker SET revision='r1'",
            "COMMIT",
        ]
        for query in queries:
            con.execute(query)
        after = state(con)
        assert (after["total"], after["finance"], after["summary"], after["release"]) == (
            360,
            8,
            360,
            "r1",
        )
        assert after["snapshot"] == binding["before"]["snapshot"] + 1
        assert captures(con) == binding["captures"]
    verify_capture(binding["selected"])
    return {"sql": queries, "after": after}


def notebook_open():
    """Require the controller's exact expected input before opening the analytical client."""
    import marimo
    from marimo._config.manager import get_default_config_manager

    case = os.environ["T44A_NOTEBOOK_CASE"]
    assert case in {"positive", "stale", "catalog"}
    marker = {"case": case, "analytical_query_started": False, "status": "FAIL"}
    try:
        data = Path("/work/notebook-binding.json").read_bytes()
        assert digest(data) == os.environ["T44A_BINDING_SHA256"], "REFUSE_STALE_BINDING"
        binding = json.loads(data)
        assert binding["notebook_sha256"] == digest(
            Path("/scripts/captured_notebook.py").read_bytes()
        )
        assert binding["sql"] == SQL and binding["sql_sha256"] == digest(SQL.encode())
        assert (
            digest(Path("/work/producer-capture.json").read_bytes())
            == binding["producer_report_sha256"]
        )
        verify_capture(binding["selected"], os.environ["T44A_SELECTED_SCHEMA"])
        assert marimo.__version__ == "0.25.2" and os.environ["T44A_PROCESS_AUDIT"] == "1"
        config = get_default_config_manager(
            current_path="/scripts/captured_notebook.py"
        ).get_config()
        assert config["completion"]["copilot"] is False
        assert all(v.get("enabled") is False for v in config["language_servers"].values())
        con = connection()
        assert captures(con) == binding["captures"]
        with pg_connection() as metadata:
            working = binding["working"]
            assert pg(metadata, "SELECT oid::bigint FROM pg_namespace WHERE nspname='public'") == [
                (working["namespace_oid"],)
            ]
            actual = pg(
                metadata,
                "SELECT table_id,table_uuid::text FROM public.ducklake_table ORDER BY table_id",
            )
            assert json.loads(json.dumps(actual)) == working["tables"]
            stored = dict(
                pg(metadata, "SELECT key,value FROM public.ducklake_metadata WHERE scope IS NULL")
            )
            assert (
                stored["version"] == "1.1"
                and Path(stored["data_path"]) == Path(working["root"]) == DATA
            )
            views = pg(
                metadata,
                "SELECT view_id,view_uuid::text,sql FROM public.ducklake_view WHERE end_snapshot IS NULL ORDER BY view_id",
            )
            assert json.loads(json.dumps(views)) == working["views"]
        marker.update(
            status="BOUND",
            kernel_pid=os.getpid(),
            kernel_ppid=os.getppid(),
            kernel_pgid=os.getpgrp(),
            kernel_session=os.getsid(0),
            connection_identity=id(con),
            kernel_start=Path("/proc/self/stat").read_text().split()[21],
            execution_uuid=binding["execution_uuid"],
        )
        save("notebook-" + case + ".json", marker)
        return binding, con, marker
    except Exception as error:
        marker.update(error_type=type(error).__name__, error=str(error))
        save("notebook-" + case + ".json", marker)
        raise


def cell_read(binding, con, marker, previous=None):
    """Run real native statements in dependent cells of the same live notebook kernel."""
    assert marker["kernel_pid"] == os.getpid() and marker["connection_identity"] == id(con)
    marker["analytical_query_started"] = True
    started = time.monotonic()
    save("notebook-positive.json", marker)
    row = con.execute(SQL).fetchone()
    expected = (
        (350, binding["before"]["snapshot"], binding["before"]["snapshot"], "r0", "r0")
        if previous is None
        else (360, previous[1] + 1, previous[1] + 1, "r1", "r1")
    )
    assert row == expected, "Actual notebook native held/fresh result differs"
    marker["held" if previous is None else "fresh"] = row
    marker.setdefault("executions", []).append(
        {
            "sql_sha256": digest(SQL.encode()),
            "started_monotonic": started,
            "completed_monotonic": time.monotonic(),
            "result": row,
        }
    )
    if previous is not None:
        marker["status"] = "PASS_REAL_MARIMO_HELD_350_FRESH_360"
        con.close()
    save("notebook-positive.json", marker)
    return row
