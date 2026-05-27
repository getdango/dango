"""tests/unit/test_notebook_lifecycle.py

Tests for notebook lifecycle integration — snapshot creation on lock,
web route snapshot passing, upload read-only connection.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.exceptions import AuthenticationError, AuthorizationError, DangoError, ValidationError
from dango.utils.dango_db import _schema_initialized, connect
from dango.web.routes.notebooks import router

_P = "dango.web.routes.notebooks"


def _make_user(role: Role = Role.ADMIN, email: str = "test@test.com") -> User:
    return User(id="u-test-1", email=email, password_hash="hashed", role=role, is_active=True)


def _make_app(project_root: Path) -> FastAPI:
    app = FastAPI()
    app.state.project_root = project_root
    status_map: dict[type[DangoError], int] = {
        AuthenticationError: 401,
        AuthorizationError: 403,
        ValidationError: 400,
        DangoError: 500,
    }

    @app.exception_handler(DangoError)
    async def _handler(request: Request, exc: DangoError) -> JSONResponse:
        code = 500
        for cls in type(exc).__mro__:
            if cls in status_map:
                code = status_map[cls]
                break
        return JSONResponse(
            status_code=code, content={"error_code": exc.error_code, "message": exc.user_message}
        )

    app.include_router(router)
    return app


def _client(tmp_path: Path, role: Role = Role.ADMIN, email: str = "test@test.com") -> TestClient:
    from typing import Any

    user = _make_user(role, email=email)
    app = _make_app(tmp_path)

    @app.middleware("http")
    async def _set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


def _init_db(tmp_path: Path) -> None:
    _schema_initialized.clear()
    with connect(tmp_path):
        pass


def _seed_notebook(tmp_path: Path, name: str, created_by: str = "test@test.com") -> None:
    nb_dir = tmp_path / "notebooks"
    nb_dir.mkdir(parents=True, exist_ok=True)
    (nb_dir / f"{name}.py").write_text("# notebook\n")
    now = datetime.now().isoformat()
    with connect(tmp_path) as conn:
        conn.execute(
            "INSERT INTO notebook_metadata (id, name, description, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), name, "Test notebook", created_by, now, now),
        )
        conn.commit()


@pytest.mark.unit
class TestLockCreatesSnapshot:
    """Verify lock_notebook() creates a DuckDB snapshot."""

    @patch(f"{_P}.start_idle_checker")
    @patch(f"{_P}.start_marimo")
    @patch(f"{_P}.get_marimo_status")
    @patch(f"{_P}.create_snapshot")
    @patch(f"{_P}.get_project_root")
    def test_lock_calls_create_snapshot(
        self,
        mock_root: MagicMock,
        mock_snapshot: MagicMock,
        mock_status: MagicMock,
        mock_start: MagicMock,
        mock_idle: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Acquiring a lock should call create_snapshot when Marimo is not running."""
        mock_root.return_value = tmp_path
        snap_path = tmp_path / ".dango" / "snapshots" / "warehouse_test_20260101.duckdb"
        mock_snapshot.return_value = snap_path
        not_running = {"running": False, "port": None, "pid": None, "log_file": None}
        running = {"running": True, "port": 7805, "pid": 123, "log_file": None}
        mock_status.side_effect = [not_running, running]
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "test_nb")

        resp = _client(tmp_path).post("/api/notebooks/test_nb/lock", json={})
        assert resp.status_code == 200
        mock_snapshot.assert_called_once_with(tmp_path, "test@test.com")

    @patch(f"{_P}.start_idle_checker")
    @patch(f"{_P}.start_marimo")
    @patch(f"{_P}.get_marimo_status")
    @patch(f"{_P}.create_snapshot", side_effect=FileNotFoundError("no warehouse"))
    @patch(f"{_P}.get_project_root")
    def test_lock_succeeds_without_warehouse(
        self,
        mock_root: MagicMock,
        mock_snapshot: MagicMock,
        mock_status: MagicMock,
        mock_start: MagicMock,
        mock_idle: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Lock should succeed even if warehouse doesn't exist (no snapshot)."""
        mock_root.return_value = tmp_path
        not_running = {"running": False, "port": None, "pid": None, "log_file": None}
        running = {"running": True, "port": 7805, "pid": 123, "log_file": None}
        mock_status.side_effect = [not_running, running]
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "test_nb")

        resp = _client(tmp_path).post("/api/notebooks/test_nb/lock", json={})
        assert resp.status_code == 200
        assert resp.json()["locked"] is True

    @patch(f"{_P}.start_idle_checker")
    @patch(f"{_P}.get_marimo_status")
    @patch(f"{_P}.create_snapshot")
    @patch(f"{_P}.get_project_root")
    def test_lock_skips_snapshot_when_marimo_already_running(
        self,
        mock_root: MagicMock,
        mock_snapshot: MagicMock,
        mock_status: MagicMock,
        mock_idle: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Snapshot creation is skipped when Marimo is already running."""
        mock_root.return_value = tmp_path
        mock_status.return_value = {"running": True, "port": 7805, "pid": 123, "log_file": None}
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "test_nb")

        resp = _client(tmp_path).post("/api/notebooks/test_nb/lock", json={})
        assert resp.status_code == 200
        mock_snapshot.assert_not_called()

    @patch(f"{_P}.start_idle_checker")
    @patch(f"{_P}.start_marimo")
    @patch(f"{_P}.get_marimo_status")
    @patch(f"{_P}.create_snapshot")
    @patch(f"{_P}.get_project_root")
    def test_lock_passes_snapshot_to_start_marimo(
        self,
        mock_root: MagicMock,
        mock_snapshot: MagicMock,
        mock_status: MagicMock,
        mock_start: MagicMock,
        mock_idle: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When Marimo isn't running, start_marimo receives snapshot_path."""
        mock_root.return_value = tmp_path
        snap_path = tmp_path / ".dango" / "snapshots" / "warehouse_test.duckdb"
        mock_snapshot.return_value = snap_path
        not_running = {"running": False, "port": None, "pid": None, "log_file": None}
        running = {"running": True, "port": 7805, "pid": 123, "log_file": None}
        mock_status.side_effect = [not_running, running]
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "test_nb")

        resp = _client(tmp_path).post("/api/notebooks/test_nb/lock", json={})
        assert resp.status_code == 200
        mock_start.assert_called_once_with(tmp_path, snapshot_path=snap_path)
