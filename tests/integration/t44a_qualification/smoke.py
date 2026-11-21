"""tests/integration/t44a_qualification/smoke.py

Representative Python permissions, namespace and PostgreSQL readiness.
"""

import json
import os
import socket
import sys
from pathlib import Path


def main():
    """Exercise intended populated mounts and actual authentication before dbt."""
    assert sys.version_info[:3] == (3, 14, 10)
    assert (os.getuid(), os.getgid()) == (501, 20)
    for folder in [
        "/source",
        "/tools",
        "/scripts",
        "/secrets",
        *(["/fixture", "/fixture/data"] if sys.argv[1] == "basic" else []),
    ]:
        assert (Path(folder) / "readiness").read_text() == "populated\n"
        try:
            (Path(folder) / "must-refuse").write_text("bad")
        except PermissionError:
            pass
        except OSError as error:
            assert error.errno == 30  # Read-only filesystem.
        else:
            raise AssertionError("Read-only write unexpectedly succeeded")
    for folder in [
        "/work",
        "/home/probe",
        *(["/fixture", "/fixture/data"] if sys.argv[1] == "postgres" else []),
    ]:
        p = Path(folder) / "readiness-write"
        p.write_text("populated")
        assert p.read_text() == "populated"
        p.unlink()
    assert [name for _, name in socket.if_nameindex()] == ["lo"]
    assert len(Path("/proc/net/route").read_text().strip().splitlines()) == 1
    with socket.socket() as connection:
        connection.settimeout(2)
        try:
            connection.connect(("198.51.100.1", 80))
        except OSError as error:
            assert error.errno in {101, 113}, str(error)
        else:
            raise AssertionError("Numeric external egress succeeded")
    if sys.argv[1] == "postgres":
        import duckdb

        os.environ["PGPASSFILE"] = "/secrets/pgpass-python"
        with duckdb.connect(config={"memory_limit": "256MB", "threads": "2"}) as pg:
            pg.execute("LOAD postgres_scanner")
            pg.execute(
                "ATTACH 'host=127.0.0.1 port=5432 dbname=probe user=postgres' "
                "AS readiness_pg (TYPE POSTGRES)"
            )
            assert pg.execute(
                "SELECT * FROM postgres_query('readiness_pg', "
                "'SELECT 1 AS n, current_user::text AS actor, current_database()::text AS db')"
            ).fetchone() == (1, "postgres", "probe")
            rules = pg.execute(
                "SELECT * FROM postgres_query('readiness_pg', "
                "$$SELECT auth_method FROM pg_hba_file_rules WHERE type='host'$$)"
            ).fetchall()
            assert rules and all(row == ("scram-sha-256",) for row in rules)
    print(json.dumps({"readiness": "PASS", "mode": sys.argv[1], "python": sys.version}))


if __name__ == "__main__":
    main()
