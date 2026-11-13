"""tests/unit/test_g4f3_file_script_upload.py

File-source delete is one transaction under DbtLock, script names are validated against
the resolved scripts directory, and uploads are streamed against a byte cap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import duckdb
import pytest
import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from dango.auth.models import Role, User
from dango.exceptions import DbtLockError
from dango.utils.dbt_lock import DbtLock
from dango.web.routes.scripts_helpers import _validate_script_path
from dango.web.routes.upload import router as upload_router
from tests.unit.test_g4f2_csv_delete_guard import _client as _delete_client
from tests.unit.test_g4f2_csv_delete_guard import _delete, _project


@pytest.fixture(autouse=True)
def _stop_patches() -> Any:
    yield
    patch.stopall()


class _MetadataDeleteFails:
    """A real DuckDB connection whose metadata DELETE fails after the row DELETE succeeded."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if "DELETE FROM _dango_file_metadata" in sql:
            raise duckdb.Error("metadata delete failed")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _rows(root: Path) -> list[tuple[Any, ...]]:
    conn = duckdb.connect(str(root / "data" / "warehouse.duckdb"), read_only=True)
    try:
        return conn.execute('SELECT _dango_filename FROM "raw_sales"."sales" ORDER BY 1').fetchall()
    finally:
        conn.close()


@pytest.mark.unit
class TestFileDeleteIsAtomicAndLocked:
    def test_failed_metadata_delete_does_not_lose_the_rows(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        client = _delete_client(root, pattern="*.csv")
        real_connect = duckdb.connect

        def connect(*args: Any, **kwargs: Any) -> _MetadataDeleteFails:
            return _MetadataDeleteFails(real_connect(*args, **kwargs))

        with patch("dango.web.routes.upload.duckdb.connect", side_effect=connect):
            resp = _delete(client, root / "data" / "synced.csv")

        assert resp.status_code >= 500
        assert (root / "data" / "synced.csv").exists()
        # rows and metadata stay consistent: nothing was removed
        assert _rows(root) == [("other.csv",), ("synced.csv",)]

    def test_delete_runs_under_the_dbt_lock(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        client = _delete_client(root, pattern="*.csv")
        real_connect = duckdb.connect
        seen: dict[str, bool] = {}

        def connect(*args: Any, **kwargs: Any) -> Any:
            other = DbtLock(root, source="probe", operation="probe")
            try:
                other.acquire(timeout=0)
            except DbtLockError:
                seen["held"] = True
            else:
                other.release()
                seen["held"] = False
            return real_connect(*args, **kwargs)

        with patch("dango.web.routes.upload.duckdb.connect", side_effect=connect):
            resp = _delete(client, root / "data" / "synced.csv")

        assert resp.status_code == 200
        assert seen == {"held": True}
        # released again afterwards
        after = DbtLock(root, source="probe", operation="probe")
        after.acquire()
        after.release()

    def test_a_running_sync_blocks_the_delete(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        client = _delete_client(root, pattern="*.csv")
        sync = DbtLock(root, source="sync", operation="load:sales")
        sync.acquire()
        try:
            resp = _delete(client, root / "data" / "synced.csv")
        finally:
            sync.release()
        assert resp.status_code == 409
        assert (root / "data" / "synced.csv").exists()
        assert _rows(root) == [("other.csv",), ("synced.csv",)]


@pytest.mark.unit
class TestScriptPathValidation:
    @staticmethod
    def _scripts(root: Path) -> None:
        (root / "scripts" / "sub").mkdir(parents=True)
        (root / "scripts" / "_private").mkdir()
        (root / "scripts_old").mkdir()
        for rel in (
            "scripts/etl.py",
            "scripts/sub/etl.py",
            "scripts/_helper.py",
            "scripts/__init__.py",
            "scripts/.hidden.py",
            "scripts/_private/x.py",
            "scripts/notes.txt",
            "scripts_old/x.py",
        ):
            (root / rel).write_text("print('x')\n")

    @pytest.mark.parametrize("name", ["etl.py", "sub/etl.py"])
    def test_discovered_scripts_validate(self, tmp_path: Path, name: str) -> None:
        self._scripts(tmp_path)
        result = _validate_script_path(tmp_path, name)
        assert isinstance(result, Path)
        assert result == (tmp_path / "scripts" / name).resolve()

    @pytest.mark.parametrize(
        "name",
        [
            "../scripts_old/x.py",
            "_helper.py",
            "__init__.py",
            ".hidden.py",
            "_private/x.py",
            "notes.txt",
            "../etc/passwd",
        ],
    )
    def test_everything_else_is_refused(self, tmp_path: Path, name: str) -> None:
        self._scripts(tmp_path)
        result = _validate_script_path(tmp_path, name)
        assert isinstance(result, JSONResponse)
        assert result.status_code in (400, 404)

    def test_a_symlink_out_of_scripts_is_refused(self, tmp_path: Path) -> None:
        self._scripts(tmp_path)
        (tmp_path / "scripts" / "link.py").symlink_to(tmp_path / "scripts_old" / "x.py")
        result = _validate_script_path(tmp_path, "link.py")
        assert isinstance(result, JSONResponse)


_CAP = 1000
_CHUNK = 256


def _upload_client(root: Path) -> TestClient:
    sources = {
        "sources": [
            {
                "name": "sales",
                "type": "csv",
                "csv": {"directory": "data", "file_pattern": "*.csv"},
            }
        ]
    }
    (root / ".dango").mkdir(exist_ok=True)
    (root / ".dango" / "sources.yml").write_text(yaml.safe_dump(sources))
    app = FastAPI()
    app.state.project_root = root
    app.include_router(upload_router)

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = User(email="ed@example.com", role=Role.EDITOR)
        request.state.auth_method = "session"
        return await call_next(request)

    for p in (
        patch("dango.web.routes.upload.get_project_root", return_value=root),
        patch("dango.web.routes.upload.MAX_UPLOAD_BYTES", _CAP, create=True),
        patch("dango.web.routes.upload._UPLOAD_CHUNK_BYTES", _CHUNK, create=True),
        patch("dango.web.routes.upload.ws_manager.broadcast", new_callable=AsyncMock),
        patch("dango.web.routes.upload.append_log_entry"),
    ):
        p.start()
    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient, size: int) -> Any:
    return client.post(
        "/api/sources/sales/upload-csv",
        files={"file": ("big.csv", b"x" * size, "text/csv")},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )


@pytest.mark.unit
class TestUploadIsStreamed:
    def test_over_the_cap_is_aborted_early_and_leaves_nothing(self, tmp_path: Path) -> None:
        client = _upload_client(tmp_path)
        reads: list[int | None] = []
        original = UploadFile.read

        async def recording_read(self: UploadFile, size: int = -1) -> bytes:
            reads.append(size)
            return await original(self, size)

        with patch.object(UploadFile, "read", recording_read):
            resp = _post(client, 50 * _CAP)

        assert resp.status_code == 413
        assert not (tmp_path / "data" / "big.csv").exists()
        # never the whole body at once: bounded chunks, and reading stopped soon after the cap
        assert reads and all(n == _CHUNK for n in reads)
        assert sum(reads) <= _CAP + 2 * _CHUNK

    def test_exactly_the_cap_is_accepted(self, tmp_path: Path) -> None:
        client = _upload_client(tmp_path)
        resp = _post(client, _CAP)
        assert resp.status_code == 200, resp.text
        assert (tmp_path / "data" / "big.csv").stat().st_size == _CAP

    def test_one_byte_over_the_cap_is_refused(self, tmp_path: Path) -> None:
        client = _upload_client(tmp_path)
        resp = _post(client, _CAP + 1)
        assert resp.status_code == 413
        assert not (tmp_path / "data" / "big.csv").exists()
