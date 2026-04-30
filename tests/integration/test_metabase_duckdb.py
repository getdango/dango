"""tests/integration/test_metabase_duckdb.py

Integration tests for DuckDB + Metabase JDBC driver version alignment.

Tests validate:
  - Python DuckDB and driver share the same major.minor
  - DuckDB read-write → read-only roundtrip works (BUG-116 regression)
  - DuckDB write + read-only lock sharing works (BUG-126 regression)
  - Full Metabase container reads Python-written DuckDB data (Docker required)
"""

from __future__ import annotations

import multiprocessing
import os
import shutil
from typing import Any

import duckdb
import pytest

from dango.utils.driver import METABASE_DUCKDB_DRIVER_VERSION


@pytest.mark.integration
class TestDuckdbVersionMatchesDriver:
    """Verify installed DuckDB Python matches the JDBC driver major.minor."""

    def test_major_minor_match(self) -> None:
        python_mm = ".".join(duckdb.__version__.split(".")[:2])
        driver_mm = ".".join(METABASE_DUCKDB_DRIVER_VERSION.split(".")[:2])
        assert python_mm == driver_mm, (
            f"DuckDB Python {duckdb.__version__} (major.minor {python_mm}) "
            f"does not match driver {METABASE_DUCKDB_DRIVER_VERSION} "
            f"(major.minor {driver_mm})"
        )


@pytest.mark.integration
class TestDuckdbReadWriteReadOnly:
    """Verify DuckDB read-only mode can read files written by the same version.

    This is the exact failure mode of BUG-116: Metabase (read-only) showed
    0 tables because the DuckDB versions were mismatched.
    """

    def test_write_then_readonly_sees_data(self, tmp_path) -> None:
        db_path = str(tmp_path / "test.duckdb")

        # Write data
        con = duckdb.connect(db_path)
        con.execute("CREATE TABLE test_table (id INTEGER, name VARCHAR)")
        con.execute("INSERT INTO test_table VALUES (1, 'alice'), (2, 'bob')")
        con.close()

        # Read back in read-only mode (how Metabase connects)
        ro_con = duckdb.connect(db_path, read_only=True)
        tables = ro_con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert "test_table" in table_names

        rows = ro_con.execute("SELECT COUNT(*) FROM test_table").fetchone()
        assert rows is not None
        assert rows[0] == 2
        ro_con.close()


def _read_readonly_in_subprocess(db_path: str, result_queue: multiprocessing.Queue[Any]) -> None:
    """Helper: open DuckDB read-only in a child process and report row count."""
    import duckdb as _duckdb

    try:
        con = _duckdb.connect(db_path, read_only=True)
        row = con.execute("SELECT COUNT(*) FROM shared_table").fetchone()
        result_queue.put(("ok", row[0] if row else 0))
        con.close()
    except Exception as exc:
        result_queue.put(("error", str(exc)))


@pytest.mark.integration
class TestDuckdbCrossProcessReadOnly:
    """Verify read-only access works from a separate process after write (BUG-126).

    DuckDB is single-writer — concurrent write + read-only is not supported.
    The real pattern is sequential: dlt writes and closes, then Metabase
    (separate JVM process) opens read-only. This test validates that
    cross-process read-only access works with matched DuckDB versions.
    """

    def test_write_close_then_readonly_subprocess(self, tmp_path) -> None:
        db_path = str(tmp_path / "test.duckdb")

        # Write data and close (simulates completed dlt sync)
        write_con = duckdb.connect(db_path)
        write_con.execute("CREATE TABLE shared_table (id INTEGER)")
        write_con.execute("INSERT INTO shared_table VALUES (1), (2), (3)")
        write_con.close()

        # Read from a child process (simulates Metabase JDBC in separate JVM)
        result_queue: multiprocessing.Queue = multiprocessing.Queue()  # type: ignore[type-arg]
        proc = multiprocessing.Process(
            target=_read_readonly_in_subprocess, args=(db_path, result_queue)
        )
        proc.start()
        proc.join(timeout=10)

        assert not result_queue.empty(), "Child process produced no result"
        status, value = result_queue.get()
        assert status == "ok", f"Child process failed: {value}"
        assert value == 3


@pytest.mark.integration
class TestMetabaseReadsPythonDuckdbData:
    """Full end-to-end: Python DuckDB → Metabase container → query.

    Requires Docker. Skipped if Docker is unavailable or
    DANGO_SKIP_DOCKER_TESTS=1.
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_docker(self) -> None:
        if os.environ.get("DANGO_SKIP_DOCKER_TESTS") == "1":
            pytest.skip("DANGO_SKIP_DOCKER_TESTS=1")
        if shutil.which("docker") is None:
            pytest.skip("Docker not available")

    def test_metabase_sees_tables(self, tmp_path) -> None:
        """Start Metabase, add DuckDB database, verify tables visible."""
        import socket
        import time

        import requests

        from dango.utils.driver import METABASE_DUCKDB_DRIVER_URL

        # 1. Write test data
        db_path = tmp_path / "warehouse.duckdb"
        con = duckdb.connect(str(db_path))
        con.execute("CREATE TABLE e2e_test (id INTEGER, value VARCHAR)")
        con.execute("INSERT INTO e2e_test VALUES (1, 'hello'), (2, 'world')")
        con.close()

        # 2. Download driver jar
        import urllib.request

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        driver_path = plugins_dir / "duckdb.metabase-driver.jar"
        urllib.request.urlretrieve(METABASE_DUCKDB_DRIVER_URL, driver_path)

        # 3. Start Metabase container (use dynamic port to avoid collisions)
        import subprocess

        # Find a free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            host_port = s.getsockname()[1]

        container_name = "dango-test-metabase-duckdb"

        # Clean up any leftover container
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
        )

        proc = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                "-p",
                f"{host_port}:3000",
                "-v",
                f"{db_path}:/data/warehouse.duckdb:ro",
                "-v",
                f"{plugins_dir}:/plugins:ro",
                "-e",
                "MB_DB_TYPE=h2",
                "-e",
                "MB_DB_FILE=/metabase-data/metabase.db",
                "metabase/metabase:v0.59.1",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"Docker run failed: {proc.stderr}"

        mb_url = f"http://localhost:{host_port}"

        try:
            # 4. Wait for Metabase health
            for _ in range(120):
                try:
                    resp = requests.get(f"{mb_url}/api/health", timeout=2)
                    if resp.status_code == 200 and resp.json().get("status") == "ok":
                        break
                except (requests.ConnectionError, requests.Timeout):
                    pass
                time.sleep(2)
            else:
                pytest.fail("Metabase did not become healthy within 240s")

            # 5. Setup admin
            setup_token_resp = requests.get(f"{mb_url}/api/session/properties", timeout=10)
            setup_token = setup_token_resp.json().get("setup-token")
            assert setup_token, "No setup token — Metabase already configured?"

            setup_resp = requests.post(
                f"{mb_url}/api/setup",
                json={
                    "token": setup_token,
                    "user": {
                        "email": "test@dango.dev",
                        "password": "Test1234!@#$",
                        "first_name": "Test",
                        "last_name": "User",
                        "site_name": "Dango Test",
                    },
                    "prefs": {"site_name": "Dango Test", "site_locale": "en"},
                },
                timeout=30,
            )
            assert setup_resp.status_code == 200, f"Metabase setup failed: {setup_resp.text}"

            # Login
            login_resp = requests.post(
                f"{mb_url}/api/session",
                json={"username": "test@dango.dev", "password": "Test1234!@#$"},
                timeout=10,
            )
            assert login_resp.status_code == 200
            session_id = login_resp.json()["id"]
            headers = {"X-Metabase-Session": session_id}

            # 6. Add DuckDB database
            db_resp = requests.post(
                f"{mb_url}/api/database",
                headers=headers,
                json={
                    "name": "Test DuckDB",
                    "engine": "duckdb",
                    "details": {
                        "database_file": "/data/warehouse.duckdb",
                        "read_only": True,
                    },
                },
                timeout=30,
            )
            assert db_resp.status_code == 200, f"Add DB failed: {db_resp.text}"
            db_id = db_resp.json()["id"]

            # 7. Trigger sync and wait
            requests.post(
                f"{mb_url}/api/database/{db_id}/sync_schema",
                headers=headers,
                timeout=10,
            )

            # Wait for sync to complete
            table_names: list[str] = []
            for _ in range(30):
                time.sleep(2)
                meta_resp = requests.get(
                    f"{mb_url}/api/database/{db_id}/metadata",
                    headers=headers,
                    timeout=10,
                )
                if meta_resp.status_code == 200:
                    tables = meta_resp.json().get("tables", [])
                    table_names = [t["name"] for t in tables]
                    if "e2e_test" in table_names:
                        break
            else:
                pytest.fail(
                    f"Metabase did not find e2e_test table after sync. Tables found: {table_names}"
                )

            # 8. Verify row count via native query
            query_resp = requests.post(
                f"{mb_url}/api/dataset",
                headers=headers,
                json={
                    "database": db_id,
                    "type": "native",
                    "native": {"query": "SELECT COUNT(*) FROM e2e_test"},
                },
                timeout=30,
            )
            assert query_resp.status_code == 202
            rows = query_resp.json()["data"]["rows"]
            assert rows[0][0] == 2

        finally:
            # Cleanup
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
            )
