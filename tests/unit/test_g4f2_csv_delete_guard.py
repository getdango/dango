"""tests/unit/test_g4f2_csv_delete_guard.py

DELETE /api/sources/<name>/csv-files removes only source input files: never the warehouse,
its sidecars, application state or files outside the source's formats and pattern, and never
unlinks a file when the database cleanup for it failed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import duckdb
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.exceptions import DangoError
from dango.web.routes.upload import router

_AJAX = {"X-Requested-With": "XMLHttpRequest"}


def _project(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    conn = duckdb.connect(str(data / "warehouse.duckdb"))
    conn.execute('CREATE SCHEMA "raw_sales"')
    conn.execute('CREATE TABLE "raw_sales"."sales" (id INTEGER, _dango_filename VARCHAR)')
    conn.execute("INSERT INTO \"raw_sales\".\"sales\" VALUES (1, 'synced.csv'), (2, 'other.csv')")
    conn.execute("CREATE TABLE _dango_file_metadata (source_name VARCHAR, file_path VARCHAR)")
    conn.close()
    (data / "warehouse.duckdb.wal").write_text("wal")
    (data / "synced.csv").write_text("id\n1\n")
    (data / "fresh.csv").write_text("id\n2\n")
    (data / "notes.txt").write_text("notes")
    (data / "other.db").write_text("x")
    state = tmp_path / ".dango" / "state"
    state.mkdir(parents=True)
    (state / "kept.csv").write_text("state")
    return tmp_path


def _client(
    root: Path, source_type: str = "local_files", pattern: str = "*", directory: str = "data"
) -> TestClient:
    key = "local_files" if source_type == "local_files" else "csv"
    sources = [
        {
            "name": "sales",
            "type": source_type,
            key: {"directory": directory, "file_pattern": pattern},
        }
    ]
    app = FastAPI()
    app.state.project_root = root
    app.include_router(router)

    @app.exception_handler(DangoError)
    async def _dango_error(request: Request, exc: DangoError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"message": "forbidden"})

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = User(email="ed@example.com", role=Role.EDITOR)
        request.state.auth_method = "session"
        return await call_next(request)

    config = MagicMock()
    config.platform.duckdb_path = "data/warehouse.duckdb"
    loader = MagicMock()
    loader.return_value.load_config.return_value = config
    app.state._patches = [
        patch("dango.web.routes.upload.get_project_root", return_value=root),
        patch("dango.web.routes.upload.load_sources_config", return_value=sources),
        patch("dango.config.ConfigLoader", loader),
        patch("dango.web.routes.upload.ws_manager.broadcast", new_callable=AsyncMock),
        patch("dango.web.routes.upload.run_dbt_after_delete", new_callable=AsyncMock),
        patch("dango.web.routes.upload.append_log_entry"),
    ]
    for p in app.state._patches:
        p.start()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _stop_patches() -> Any:
    yield
    patch.stopall()


def _delete(client: TestClient, path: Path) -> Any:
    return client.delete(
        "/api/sources/sales/csv-files", params={"file_path": str(path)}, headers=_AJAX
    )


@pytest.mark.unit
class TestProtectedPaths:
    @pytest.mark.parametrize(
        "target",
        ["data/warehouse.duckdb", "data/warehouse.duckdb.wal"],
    )
    def test_warehouse_and_sidecar_are_refused_in_a_broad_directory(
        self, tmp_path: Path, target: str
    ) -> None:
        root = _project(tmp_path)
        resp = _delete(_client(root), root / target)
        assert resp.status_code in (400, 403)
        assert (root / target).exists()

    def test_application_state_is_refused(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        resp = _delete(_client(root, directory="."), root / ".dango" / "state" / "kept.csv")
        assert resp.status_code in (400, 403)
        assert (root / ".dango" / "state" / "kept.csv").exists()

    def test_symlink_to_the_warehouse_is_refused(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        link = root / "data" / "link.csv"
        link.symlink_to(root / "data" / "warehouse.duckdb")
        resp = _delete(_client(root), link)
        assert resp.status_code in (400, 403)
        assert (root / "data" / "warehouse.duckdb").exists()


@pytest.mark.unit
class TestOnlySourceInputFiles:
    def test_file_outside_the_source_pattern_is_refused(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        resp = _delete(
            _client(root, source_type="csv", pattern="*.csv"), root / "data" / "notes.txt"
        )
        assert resp.status_code in (400, 403)
        assert (root / "data" / "notes.txt").exists()

    def test_unsupported_format_is_refused_even_when_the_pattern_matches(
        self, tmp_path: Path
    ) -> None:
        root = _project(tmp_path)
        resp = _delete(_client(root, pattern="*"), root / "data" / "other.db")
        assert resp.status_code in (400, 403)
        assert (root / "data" / "other.db").exists()

    def test_matching_file_is_deleted_with_its_rows(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        resp = _delete(_client(root, pattern="*.csv"), root / "data" / "synced.csv")
        assert resp.status_code == 200
        assert not (root / "data" / "synced.csv").exists()
        conn = duckdb.connect(str(root / "data" / "warehouse.duckdb"), read_only=True)
        rows = conn.execute('SELECT _dango_filename FROM "raw_sales"."sales"').fetchall()
        conn.close()
        assert rows == [("other.csv",)]

    def test_never_synced_file_is_still_deletable(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        conn = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
        conn.execute("DROP TABLE _dango_file_metadata")
        conn.execute('DROP SCHEMA "raw_sales" CASCADE')
        conn.close()
        resp = _delete(_client(root, pattern="*.csv"), root / "data" / "fresh.csv")
        assert resp.status_code == 200
        assert not (root / "data" / "fresh.csv").exists()


@pytest.mark.unit
class TestFailedCleanupKeepsTheFile:
    def test_unopenable_warehouse_keeps_the_file(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        client = _client(root, pattern="*.csv")
        with patch("dango.web.routes.upload.duckdb.connect", side_effect=duckdb.IOException("x")):
            resp = _delete(client, root / "data" / "synced.csv")
        assert resp.status_code >= 500
        assert (root / "data" / "synced.csv").exists()

    def test_failed_row_delete_keeps_the_file(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        client = _client(root, pattern="*.csv")
        conn = MagicMock()
        conn.execute.side_effect = duckdb.Error("cleanup failed")
        with patch("dango.web.routes.upload.duckdb.connect", return_value=conn):
            resp = _delete(client, root / "data" / "synced.csv")
        assert resp.status_code >= 500
        assert (root / "data" / "synced.csv").exists()


@pytest.mark.unit
class TestDeletionSetIsTheLoadersFileSet:
    """Only files that glob(directory / file_pattern) yields, in a format the loader reads."""

    def test_nested_file_a_non_recursive_pattern_never_reads_is_refused(
        self, tmp_path: Path
    ) -> None:
        root = _project(tmp_path)
        nested = root / "data" / "uploads" / "other"
        nested.mkdir(parents=True)
        (nested / "b.csv").write_text("id\n1\n")
        resp = _delete(_client(root, pattern="*.csv"), nested / "b.csv")
        assert resp.status_code in (400, 403)
        assert (nested / "b.csv").exists()

    def test_csv_under_an_unrelated_directory_of_a_root_source_is_refused(
        self, tmp_path: Path
    ) -> None:
        root = _project(tmp_path)
        seeds = root / "dbt" / "seeds"
        seeds.mkdir(parents=True)
        (seeds / "s.csv").write_text("id\n1\n")
        resp = _delete(_client(root, pattern="*.csv", directory="."), seeds / "s.csv")
        assert resp.status_code in (400, 403)
        assert (seeds / "s.csv").exists()

    def test_recursive_pattern_files_are_deletable(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        nested = root / "data" / "sub"
        nested.mkdir()
        (nested / "c.csv").write_text("id\n1\n")
        resp = _delete(_client(root, pattern="**/*.csv"), nested / "c.csv")
        assert resp.status_code == 200
        assert not (nested / "c.csv").exists()

    def test_parquet_input_of_a_csv_source_with_a_parquet_pattern_is_deletable(
        self, tmp_path: Path
    ) -> None:
        root = _project(tmp_path)
        (root / "data" / "p.parquet").write_text("parquet")
        resp = _delete(
            _client(root, source_type="csv", pattern="*.parquet"), root / "data" / "p.parquet"
        )
        assert resp.status_code == 200
        assert not (root / "data" / "p.parquet").exists()
