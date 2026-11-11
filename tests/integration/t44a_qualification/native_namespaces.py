"""tests/integration/t44a_qualification/native_namespaces.py

Native actor-catalog independence; no authenticated-user or Dango tenancy claim.
"""

import hashlib
import json
from pathlib import Path

from dbt_assets import MIB, allocated

EXTRA_PEAK = 128 * MIB
ACTORS = {
    name: ("t44a_actor_" + name, Path("/fixture/data/actor-" + name)) for name in ("alice", "bob")
}
TABLES = [("finance", "orders", "BASE TABLE"), ("sales", "orders", "BASE TABLE")]


def bound_file(root, parts):
    """Resolve the actual catalog's schema/table/file paths within one owned root."""
    assert not root.is_symlink() and root.resolve(strict=True) == root
    path = root
    for text, relative in parts:
        part = Path(text)
        assert isinstance(relative, bool) and ".." not in part.parts
        assert not relative or not part.is_absolute(), "Contradictory catalog relative path"
        path = path / part if relative else part
        assert path.is_absolute() and path.is_relative_to(root), "Catalog file escapes actor root"
        for component in [path, *path.parents]:
            if component == root:
                break
            assert not component.is_symlink(), "Actor catalog path contains symlink"
    assert path.resolve() == path, "Noncanonical actor catalog path"
    return path


def require_rows(state, rows, finance):
    """Check complete independent expected rows, keys and same-named schemas."""
    assert state["tables"] == TABLES, "Actor must retain exact sales.orders and finance.orders"
    assert state["sales"] == rows and len(rows) == len({r[0] for r in rows}) == 3
    assert state["finance"] == [(finance,)]
    assert state["tuple"] == [sum(r[2] for r in rows), finance]


def require_independent(states):
    """Use actual namespace OIDs, roots and table UUIDs, not local integer IDs/labels."""
    alice, bob = states["alice"], states["bob"]
    assert alice["namespace_oid"] != bob["namespace_oid"], "Shared actor metadata namespace"
    assert alice["data_root"] != bob["data_root"], "Shared actor data root"
    assert alice["root_identity"] != bob["root_identity"], "Shared physical actor directory"
    identities = [{r[3] for r in state["table_ids"]} for state in (alice, bob)]
    assert all(isinstance(value, str) and value for ids in identities for value in ids), (
        "Missing actor table UUID"
    )
    assert all(len(ids) == 2 for ids in identities) and identities[0].isdisjoint(identities[1]), (
        "Shared or missing actor table UUIDs"
    )


def run_namespaces(metadata):
    """Create actual actor lakes, mutate both ways, and observe each fresh binding."""
    import duckdb

    assert duckdb.__version__ == "2.0.0" and "password=" not in metadata.lower()
    report = {
        "status": "FAIL",
        "phases": {},
        "sql": [],
        "engine_package": duckdb.__version__,
        "scope": "Catalog/data independence only; same UID/PG user, no security/tenancy or Dango integration proof",
    }
    secrets = list(json.loads(Path("/secrets/catalog.json").read_text()).values())

    def execute(connection, statement):
        entry = {"sql": statement}
        report["sql"].append(entry)
        try:
            value = connection.execute(statement)
            entry["status"] = "executed"
            return value
        except duckdb.Error as error:
            entry.update(status="engine_error", type=type(error).__name__, message=str(error))
            raise

    def pg(query):
        with duckdb.connect(config={"memory_limit": "256MB"}) as connection:
            connection.execute("LOAD postgres_scanner")
            connection.execute(
                f"ATTACH '{metadata.removeprefix('postgres:')}' AS pg (TYPE POSTGRES)"
            )
            return connection.execute(
                "SELECT * FROM postgres_query('pg', $$" + query + "$$)"
            ).fetchall()

    def connect(actor, read_only=True):
        schema, root = ACTORS[actor]
        connection = duckdb.connect(config={"memory_limit": "512MB", "threads": "2"})
        try:
            options = f"METADATA_SCHEMA '{schema}', DATA_PATH '{root}', DUCKLAKE_VERSION '1.1', "
            options += "AUTOMATIC_MIGRATION FALSE, DATA_INLINING_ROW_LIMIT 0"
            if read_only:
                options += ", READ_ONLY"
            connection.execute(f"ATTACH 'ducklake:{metadata}' AS {actor} ({options})")
            return connection
        except Exception:
            connection.close()
            raise

    def original():
        with duckdb.connect(config={"memory_limit": "256MB"}) as connection:
            connection.execute(
                f"ATTACH 'ducklake:{metadata}' AS original (READ_ONLY, AUTOMATIC_MIGRATION FALSE)"
            )
            connection.execute("BEGIN")
            rows = connection.execute("SELECT * FROM original.sales.orders ORDER BY ALL").fetchall()
            assert [(r[0], r[2]) for r in rows] == [(1, 100), (2, 200), (3, 50)]
            assert connection.execute(
                "SELECT sum(amount) FROM original.sales.orders_view"
            ).fetchone() == (350,)
            assert connection.execute(
                "SELECT sum(amount) FROM original.sales.orders WHERE order_date >= DATE '2026-09-01' AND order_date < DATE '2026-10-01'"
            ).fetchone() == (300,)
            snapshots = pg("SELECT snapshot_id FROM public.ducklake_snapshot ORDER BY snapshot_id")
            connection.execute("COMMIT")
        return {"rows": rows, "snapshot_ids": snapshots}

    def inventory(actor):
        schema, root = ACTORS[actor]
        stored = pg(
            f"SELECT key,value FROM {schema}.ducklake_metadata WHERE scope IS NULL AND key IN ('version','data_path')"
        )
        assert dict(stored)["version"] == "1.1"
        assert Path(dict(stored)["data_path"]) == root, "Native catalog data root differs"
        files = {}
        active_tables = set()
        for kind, table in (("data", "ducklake_data_file"), ("delete", "ducklake_delete_file")):
            rows = pg(
                f"SELECT s.schema_name,t.table_name,s.path,s.path_is_relative,"
                f"t.path,t.path_is_relative,f.path,f.path_is_relative,f.file_size_bytes,f.end_snapshot "
                f"FROM {schema}.{table} f JOIN {schema}.ducklake_table t USING(table_id) "
                f"JOIN {schema}.ducklake_schema s USING(schema_id) "
                "WHERE t.end_snapshot IS NULL AND s.end_snapshot IS NULL ORDER BY f.path"
            )
            for name, relation, sp, sr, tp, tr, fp, fr, size, end in rows:
                assert (name, relation) in {("sales", "orders"), ("finance", "orders")}
                path = bound_file(root, [(sp, sr), (tp, tr), (fp, fr)])
                if not path.exists():
                    assert end is not None, "Missing live actor data file"
                    continue
                assert (
                    path.is_file() and path.suffix == ".parquet" and path.stat().st_size == size > 0
                )
                relative = str(path.relative_to(root))
                assert relative not in files, "Ambiguous catalog file binding"
                files[relative] = {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": size,
                    "kind": kind,
                    "table": [name, relation],
                    "end_snapshot": end,
                }
                if kind == "data" and end is None:
                    active_tables.add((name, relation))
        assert active_tables == {("sales", "orders"), ("finance", "orders")}
        for path in root.rglob("*"):
            assert not path.is_symlink(), "Actor inventory contains symlink"
        physical = {str(p.relative_to(root)) for p in root.rglob("*.parquet")}
        assert physical == set(files) and files, "Unbound or empty actor Parquet inventory"
        return files

    def observe(actor):
        schema, root = ACTORS[actor]
        with connect(actor) as connection:
            connection.execute("BEGIN")
            value = execute(
                connection,
                f"SELECT (SELECT sum(amount) FROM {actor}.sales.orders),"
                f"(SELECT sum(amount) FROM {actor}.finance.orders)",
            ).fetchone()
            rows = execute(
                connection, f"SELECT * FROM {actor}.sales.orders ORDER BY ALL"
            ).fetchall()
            finance = execute(
                connection, f"SELECT * FROM {actor}.finance.orders ORDER BY ALL"
            ).fetchall()
            tables = connection.execute(
                "SELECT table_schema,table_name,table_type FROM information_schema.tables "
                f"WHERE table_catalog='{actor}' AND table_schema IN ('sales','finance') ORDER BY table_schema,table_name"
            ).fetchall()
            oid = pg(f"SELECT oid::bigint FROM pg_namespace WHERE nspname='{schema}'")
            assert len(oid) == 1 and oid[0][0] > 0
            identities = pg(
                f"SELECT s.schema_name,t.table_name,t.table_id,t.table_uuid::text "
                f"FROM {schema}.ducklake_table t JOIN {schema}.ducklake_schema s USING(schema_id) "
                "WHERE t.end_snapshot IS NULL AND s.end_snapshot IS NULL ORDER BY s.schema_name,t.table_name"
            )
            assert [(r[0], r[1]) for r in identities] == [
                ("finance", "orders"),
                ("sales", "orders"),
            ]
            snapshots = pg(
                f"SELECT snapshot_id FROM {schema}.ducklake_snapshot ORDER BY snapshot_id"
            )
            files = inventory(actor)
            connection.execute("COMMIT")
        return {
            "tuple": list(value),
            "sales": rows,
            "finance": finance,
            "tables": tables,
            "metadata_schema": schema,
            "namespace_oid": oid[0][0],
            "data_root": str(root),
            "root_identity": [root.stat().st_dev, root.stat().st_ino],
            "table_ids": identities,
            "snapshot_ids": snapshots,
            "files": files,
        }

    def allocation_gate():
        size = pg(
            "SELECT coalesce(sum(pg_total_relation_size(c.oid)),0) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','m') "
            "AND n.nspname IN ('t44a_actor_alice','t44a_actor_bob')"
        )[0][0]
        used = int(size) + sum(allocated(root) for _, root in ACTORS.values())
        report["data_and_metadata_upper"] = used
        assert used <= 64 * MIB, "Actor data/metadata bound; global WAL reserve unchanged"

    def phase(name, alice_rows, alice_finance, bob_rows):
        states = {actor: observe(actor) for actor in ACTORS}
        require_rows(states["alice"], alice_rows, alice_finance)
        require_rows(states["bob"], bob_rows, 7)
        require_independent(states)
        assert original() == report["original"], "Original app fixture changed"
        allocation_gate()
        report["phases"][name] = states
        return states

    def mutate(actor, statements):
        connection, active = connect(actor, False), False
        try:
            execute(connection, "BEGIN")
            active = True
            for statement in statements:
                execute(connection, statement)
            execute(connection, "COMMIT")
            active = False
        finally:
            try:
                if active:
                    execute(connection, "ROLLBACK")
            finally:
                connection.close()

    try:
        for schema, root in ACTORS.values():
            assert not root.exists() and not root.is_symlink(), "Actor root must be fresh"
            assert not pg(f"SELECT oid FROM pg_namespace WHERE nspname='{schema}'"), (
                "Actor metadata must be fresh"
            )
            root.mkdir()
            assert root.resolve(strict=True) == root
            assert (root.stat().st_uid, root.stat().st_gid) == (501, 20)
        report["original"] = original()
        rows = report["original"]["rows"]
        for actor in ACTORS:
            with connect(actor, False) as connection:
                connection.execute(
                    f"ATTACH 'ducklake:{metadata}' AS original (READ_ONLY, AUTOMATIC_MIGRATION FALSE)"
                )
                execute(connection, "BEGIN")
                for schema in ("sales", "finance"):
                    execute(connection, f"CREATE SCHEMA {actor}.{schema}")
                execute(
                    connection,
                    f"CREATE TABLE {actor}.sales.orders AS SELECT * FROM original.sales.orders",
                )
                execute(
                    connection, f"CREATE TABLE {actor}.finance.orders AS SELECT 7::INTEGER amount"
                )
                execute(connection, "COMMIT")
        baseline = phase("both350", rows, 7, rows)
        mutate("bob", ["UPDATE bob.sales.orders SET amount=amount*2"])
        doubled = [(r[0], r[1], r[2] * 2) for r in rows]
        bob_changed = phase("bob700_alice350", rows, 7, doubled)
        assert bob_changed["alice"] == baseline["alice"], "Bob mutation affected Alice"
        assert bob_changed["bob"]["table_ids"] == baseline["bob"]["table_ids"]
        assert bob_changed["bob"]["snapshot_ids"][:-1] == baseline["bob"]["snapshot_ids"]
        mutate(
            "alice",
            [
                "UPDATE alice.sales.orders SET amount=110 WHERE id=1",
                "UPDATE alice.finance.orders SET amount=8",
            ],
        )
        changed = [(r[0], r[1], 110 if r[0] == 1 else r[2]) for r in rows]
        alice_changed = phase("alice360finance8_bob700finance7", changed, 8, doubled)
        assert alice_changed["bob"] == bob_changed["bob"], "Alice mutation affected Bob"
        assert alice_changed["alice"]["table_ids"] == baseline["alice"]["table_ids"]
        assert alice_changed["alice"]["snapshot_ids"][:-1] == baseline["alice"]["snapshot_ids"]
        mutate(
            "alice",
            [
                "UPDATE alice.sales.orders SET amount=100 WHERE id=1",
                "UPDATE alice.finance.orders SET amount=7",
            ],
        )
        final = phase("explicit_alice_revert350_bob700", rows, 7, doubled)
        assert final["bob"] == bob_changed["bob"]
        assert final["alice"]["snapshot_ids"][:-1] == alice_changed["alice"]["snapshot_ids"]
        for actor in ACTORS:
            assert final[actor]["table_ids"] == baseline[actor]["table_ids"]
            assert final[actor]["namespace_oid"] == baseline[actor]["namespace_oid"]
        report["status"] = "PASS_NATIVE_ACTOR_CATALOG_INDEPENDENCE"
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        payload = json.dumps(report, indent=2, default=str) + "\n"
        assert len(payload.encode()) <= 16 * MIB and not any(s in payload for s in secrets), (
            "Actor report size/secret refusal"
        )
        Path("/work/native-namespaces.json").write_text(payload)
