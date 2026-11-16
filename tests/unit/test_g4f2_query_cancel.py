"""tests/unit/test_g4f2_query_cancel.py

The web SQL endpoint interrupts the DuckDB query on timeout or cancellation, waits for the worker
to unwind before closing the connection, and caps how many queries run at once.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.exceptions import DangoError
from dango.web.routes import query as query_module


class FakeConn:
    """Stands in for the locked DuckDB connection; execute() blocks like a long query.

    It returns on its own after ``run_seconds`` (or when ``release`` is set) unless interrupt()
    is called first (then it raises InterruptException), as DuckDB does for a cancelled query.
    """

    def __init__(self, run_seconds: float = 3.0, release: threading.Event | None = None) -> None:
        self.run_seconds = run_seconds
        self.release = release
        self.started = threading.Event()
        self.interrupted = threading.Event()
        self.closed = threading.Event()
        self.closed_while_running = False
        self._running = False

    def execute(self, sql: str) -> Any:
        self._running = True
        self.started.set()
        try:
            end = time.monotonic() + self.run_seconds
            while time.monotonic() < end:
                if self.interrupted.is_set():
                    raise duckdb.InterruptException("interrupted")
                if self.release is not None and self.release.is_set():
                    break
                time.sleep(0.01)
            result = MagicMock()
            result.description = [("n",)]
            result.fetchmany.return_value = [(1,)]
            return result
        finally:
            self._running = False

    def interrupt(self) -> None:
        self.interrupted.set()

    def close(self) -> None:
        self.closed_while_running = self._running
        self.closed.set()


def _app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.state.project_root = tmp_path

    @app.exception_handler(DangoError)
    async def _dango_error(request: Request, exc: DangoError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"message": "forbidden"})

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = User(email="ed@example.com", role=Role.EDITOR)
        request.state.auth_method = "session"
        return await call_next(request)

    app.include_router(query_module.router)
    return app


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    path = tmp_path / "warehouse.duckdb"
    path.touch()
    return path


def _patches(warehouse: Path, conns: list[FakeConn], timeout: int = 30) -> list[Any]:
    def open_conn(db_path: Any, memory_limit: str = "1GB") -> FakeConn:
        return conns.pop(0)

    return [
        patch("dango.web.routes.query.get_duckdb_path", return_value=warehouse),
        patch("dango.web.routes.query._get_api_config", return_value=(100, timeout)),
        patch("dango.web.routes.query.log_auth_event"),
        patch("dango.utils.query_connection.open_locked_query_connection", side_effect=open_conn),
    ]


def _run(warehouse: Path, conns: list[FakeConn], timeout: int, tmp_path: Path) -> Any:
    patches = _patches(warehouse, conns, timeout)
    for p in patches:
        p.start()
    try:
        client = TestClient(_app(tmp_path), raise_server_exceptions=False)
        return client.post("/api/query", json={"sql": "SELECT 1"})
    finally:
        patch.stopall()


@pytest.mark.unit
class TestTimeoutInterruptsTheQuery:
    def test_timeout_interrupts_then_closes_after_the_worker_unwinds(
        self, warehouse: Path, tmp_path: Path
    ) -> None:
        conn = FakeConn(run_seconds=3.0)
        started = time.monotonic()
        resp = _run(warehouse, [conn], timeout=1, tmp_path=tmp_path)
        assert resp.status_code == 408
        assert conn.interrupted.is_set()
        assert conn.closed.is_set()
        assert conn.closed_while_running is False
        assert time.monotonic() - started < 2.9


@pytest.mark.unit
class TestCancellationInterruptsTheQuery:
    def test_cancelled_request_interrupts_the_query(self, warehouse: Path, tmp_path: Path) -> None:
        conn = FakeConn(run_seconds=3.0)
        patches = _patches(warehouse, [conn])
        for p in patches:
            p.start()
        try:
            request = MagicMock()
            request.app.state.project_root = tmp_path
            request.client = None
            body = query_module.QueryRequest(sql="SELECT 1")
            user = User(email="ed@example.com", role=Role.EDITOR)

            async def scenario() -> None:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(query_module.execute_query(body, request, user), 0.5)

            with patch("dango.web.routes.query._get_query_memory_limit", return_value="1GB"):
                asyncio.run(scenario())
        finally:
            patch.stopall()
        assert conn.interrupted.is_set()
        assert conn.closed.is_set()


@pytest.mark.unit
class TestConcurrentQueryCap:
    def test_queries_over_the_cap_are_refused_and_slots_are_released(
        self, warehouse: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(query_module, "_MAX_CONCURRENT_QUERIES", 2)
        release = threading.Event()
        blockers = [FakeConn(run_seconds=3.0, release=release) for _ in range(2)]
        conns = [*blockers, FakeConn(run_seconds=0.0)]  # handed out (popped) in order
        patches = _patches(warehouse, conns)
        for p in patches:
            p.start()
        statuses: list[int] = []
        try:
            with TestClient(_app(tmp_path), raise_server_exceptions=False) as client:

                def fire() -> None:
                    statuses.append(client.post("/api/query", json={"sql": "SELECT 1"}).status_code)

                workers = [threading.Thread(target=fire) for _ in range(2)]
                for w in workers:
                    w.start()
                deadline = time.monotonic() + 3
                while not all(c.started.is_set() for c in blockers) and time.monotonic() < deadline:
                    time.sleep(0.01)
                over = client.post("/api/query", json={"sql": "SELECT 1"})
                release.set()
                for w in workers:
                    w.join(5)
                after = client.post("/api/query", json={"sql": "SELECT 1"})
        finally:
            patch.stopall()
        assert over.status_code == 429
        assert sorted(statuses) == [200, 200]
        assert after.status_code == 200
