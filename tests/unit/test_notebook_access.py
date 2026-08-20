"""tests/unit/test_notebook_access.py

Tests for notebook access control by role (Admin, Editor, Viewer).

Verifies that:
- Admin and Editor can list, open, and delete all notebooks regardless of creator
- Viewer is correctly restricted from mutating operations
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
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
    """Create a test user."""
    return User(id="u-test-1", email=email, password_hash="hashed", role=role, is_active=True)


def _make_app(project_root: Path) -> FastAPI:
    """Create a minimal FastAPI app with the notebooks router."""
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
        """Handle DangoError exceptions."""
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
    """Create a test client with auth middleware injecting a user."""
    user = _make_user(role, email=email)
    app = _make_app(tmp_path)

    @app.middleware("http")
    async def _set_user(request: Any, call_next: Any) -> Any:
        """Inject test user."""
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


def _init_db(tmp_path: Path) -> None:
    """Initialize the dango.db schema for tests."""
    _schema_initialized.clear()
    with connect(tmp_path):
        pass


def _seed_notebook(tmp_path: Path, name: str, created_by: str = "test@test.com") -> None:
    """Create a notebook file and metadata entry."""
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


def _seed_lock(tmp_path: Path, name: str, locked_by: str = "other@test.com") -> None:
    """Create a lock entry for a notebook."""
    with connect(tmp_path) as conn:
        conn.execute(
            "INSERT INTO notebook_locks (notebook_id, locked_by, locked_at, expires_at) "
            "VALUES (?, ?, datetime('now'), datetime('now', '+15 minutes'))",
            (name, locked_by),
        )
        conn.commit()


@pytest.mark.unit
class TestNotebookAccessControl:
    """Tests for notebook access control by role."""

    @patch(f"{_P}.get_project_root")
    def test_admin_can_list_all_notebooks(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Admin can list notebooks created by other users."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "others_notebook", created_by="other@test.com")
        resp = _client(tmp_path, role=Role.ADMIN).get("/api/notebooks")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "others_notebook"
        assert data[0]["created_by"] == "other@test.com"

    @patch(f"{_P}.get_project_root")
    def test_editor_can_list_all_notebooks(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Editor can list notebooks created by other users."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "others_notebook", created_by="other@test.com")
        resp = _client(tmp_path, role=Role.EDITOR).get("/api/notebooks")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "others_notebook"
        assert data[0]["created_by"] == "other@test.com"

    @patch(f"{_P}.get_project_root")
    def test_viewer_cannot_delete_notebook(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Viewer cannot delete any notebook."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "some_notebook", created_by="test@test.com")
        resp = _client(tmp_path, role=Role.VIEWER).delete("/api/notebooks/some_notebook")
        assert resp.status_code == 403

    @patch(f"{_P}.get_project_root")
    def test_editor_can_delete_others_notebook(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Editor can delete notebooks created by other users."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "others_notebook", created_by="other@test.com")
        resp = _client(tmp_path, role=Role.EDITOR).delete("/api/notebooks/others_notebook")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    @patch(f"{_P}.get_project_root")
    def test_admin_can_delete_any_notebook(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Admin can delete notebooks created by any user."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "others_notebook", created_by="other@test.com")
        resp = _client(tmp_path, role=Role.ADMIN).delete("/api/notebooks/others_notebook")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    @patch(f"{_P}.start_idle_checker")
    @patch(f"{_P}.start_marimo")
    @patch(f"{_P}.get_marimo_status")
    @patch(f"{_P}.get_project_root")
    def test_admin_can_open_others_notebook(
        self,
        mock_root: MagicMock,
        mock_status: MagicMock,
        mock_start: MagicMock,
        mock_idle: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Admin can open (lock) notebooks created by other users."""
        mock_root.return_value = tmp_path
        mock_status.return_value = {"running": True, "port": 7805, "pid": 123, "log_file": None}
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "others_notebook", created_by="other@test.com")
        resp = _client(tmp_path, role=Role.ADMIN).post(
            "/api/notebooks/others_notebook/lock", json={}
        )
        assert resp.status_code == 200
        assert resp.json()["locked"] is True
        assert "?file=others_notebook.py" in resp.json()["marimo_url"]

    @patch(f"{_P}.start_idle_checker")
    @patch(f"{_P}.start_marimo")
    @patch(f"{_P}.get_marimo_status")
    @patch(f"{_P}.get_project_root")
    def test_editor_can_open_others_notebook(
        self,
        mock_root: MagicMock,
        mock_status: MagicMock,
        mock_start: MagicMock,
        mock_idle: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Editor can open (lock) notebooks created by other users."""
        mock_root.return_value = tmp_path
        mock_status.return_value = {"running": True, "port": 7805, "pid": 123, "log_file": None}
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "others_notebook", created_by="other@test.com")
        resp = _client(tmp_path, role=Role.EDITOR).post(
            "/api/notebooks/others_notebook/lock", json={}
        )
        assert resp.status_code == 200
        assert resp.json()["locked"] is True
        assert "?file=others_notebook.py" in resp.json()["marimo_url"]
