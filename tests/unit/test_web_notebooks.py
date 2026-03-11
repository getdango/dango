"""tests/unit/test_web_notebooks.py

Unit tests for notebook web UI page and API endpoints.
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
class TestNotebooksPage:
    """Tests for GET /notebooks page route."""

    @patch(f"{_P}.get_project_root")
    @patch(f"{_P}._render_template")
    def test_renders_page(
        self, mock_render: MagicMock, mock_root: MagicMock, tmp_path: Path
    ) -> None:
        """Page route returns HTML response."""
        mock_root.return_value = tmp_path
        mock_render.return_value = JSONResponse(content={"html": "ok"})
        resp = _client(tmp_path).get("/notebooks")
        assert resp.status_code == 200
        mock_render.assert_called_once()
        assert mock_render.call_args[0][0] == "notebooks.html"
        assert mock_render.call_args[0][1]["current_page"] == "notebooks"

    def test_viewer_can_access(self, tmp_path: Path) -> None:
        """Viewer (notebooks.view) can access the page."""
        with patch(f"{_P}._render_template") as mock_render:
            mock_render.return_value = JSONResponse(content={"html": "ok"})
            assert _client(tmp_path, role=Role.VIEWER).get("/notebooks").status_code == 200


@pytest.mark.unit
class TestListNotebooks:
    """Tests for GET /api/notebooks."""

    @patch(f"{_P}.get_project_root")
    def test_empty_list(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns empty list when no notebooks exist."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        resp = _client(tmp_path).get("/api/notebooks")
        assert resp.status_code == 200
        assert resp.json() == []

    @patch(f"{_P}.get_project_root")
    def test_populated_list(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns notebooks with metadata and lock info."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "analysis")
        _seed_lock(tmp_path, "analysis", locked_by="editor@test.com")
        data = _client(tmp_path).get("/api/notebooks").json()
        assert len(data) == 1
        assert data[0]["name"] == "analysis"
        assert data[0]["file_exists"] is True
        assert data[0]["lock"]["locked_by"] == "editor@test.com"

    @patch(f"{_P}.get_project_root")
    def test_unregistered_file(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Files on disk without metadata appear in the list."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        nb_dir = tmp_path / "notebooks"
        nb_dir.mkdir(parents=True, exist_ok=True)
        (nb_dir / "orphan.py").write_text("# orphan\n")
        data = _client(tmp_path).get("/api/notebooks").json()
        assert len(data) == 1
        assert data[0]["name"] == "orphan"
        assert data[0]["id"] is None

    @patch(f"{_P}.get_project_root")
    def test_viewer_can_list(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Viewer can list notebooks."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        assert _client(tmp_path, role=Role.VIEWER).get("/api/notebooks").status_code == 200


@pytest.mark.unit
class TestCreateNotebook:
    """Tests for POST /api/notebooks."""

    @patch(f"{_P}._audit")
    @patch(f"{_P}.get_project_root")
    @patch(f"{_P}.shutil.copy2")
    def test_create_success(
        self, _mc: MagicMock, mock_root: MagicMock, mock_audit: MagicMock, tmp_path: Path
    ) -> None:
        """Creates notebook file, metadata entry, and logs audit event."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        (tmp_path / "notebooks").mkdir(parents=True, exist_ok=True)
        resp = _client(tmp_path).post(
            "/api/notebooks", json={"name": "my_analysis", "template": "blank"}
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "my_analysis"
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0].value == "notebook_created"

    @patch(f"{_P}.get_project_root")
    def test_duplicate_name(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns 400 for duplicate notebook name."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "existing")
        resp = _client(tmp_path).post(
            "/api/notebooks", json={"name": "existing", "template": "blank"}
        )
        assert resp.status_code == 400
        assert "already exists" in resp.json()["message"]

    @patch(f"{_P}.get_project_root")
    def test_invalid_name(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns 400 for invalid notebook name."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        resp = _client(tmp_path).post(
            "/api/notebooks", json={"name": "bad-name!", "template": "blank"}
        )
        assert resp.status_code == 400
        assert "Invalid notebook name" in resp.json()["message"]

    @patch(f"{_P}.get_project_root")
    def test_invalid_template(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns 400 for invalid template choice."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        resp = _client(tmp_path).post(
            "/api/notebooks", json={"name": "ok_name", "template": "nonexistent"}
        )
        assert resp.status_code == 400
        assert "Invalid template" in resp.json()["message"]

    @patch(f"{_P}.get_project_root")
    def test_viewer_forbidden(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Viewer cannot create notebooks."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        resp = _client(tmp_path, role=Role.VIEWER).post(
            "/api/notebooks", json={"name": "t", "template": "blank"}
        )
        assert resp.status_code == 403


@pytest.mark.unit
class TestDeleteNotebook:
    """Tests for DELETE /api/notebooks/{name}."""

    @patch(f"{_P}._audit")
    @patch(f"{_P}.get_project_root")
    def test_admin_deletes(
        self, mock_root: MagicMock, mock_audit: MagicMock, tmp_path: Path
    ) -> None:
        """Admin can delete any notebook and logs audit event."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "to_delete", created_by="other@test.com")
        resp = _client(tmp_path, role=Role.ADMIN).delete("/api/notebooks/to_delete")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0].value == "notebook_deleted"

    @patch(f"{_P}.get_project_root")
    def test_editor_deletes(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Editor (has notebooks.manage) can delete any notebook."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "to_delete", created_by="other@test.com")
        assert (
            _client(tmp_path, role=Role.EDITOR).delete("/api/notebooks/to_delete").status_code
            == 200
        )

    @patch(f"{_P}.get_project_root")
    def test_not_found(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns 404 for nonexistent notebook."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        assert _client(tmp_path).delete("/api/notebooks/nonexistent").status_code == 404

    @patch(f"{_P}.get_project_root")
    def test_viewer_forbidden(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Viewer cannot delete notebooks."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "test_nb")
        assert (
            _client(tmp_path, role=Role.VIEWER).delete("/api/notebooks/test_nb").status_code == 403
        )

    @patch(f"{_P}.get_project_root")
    def test_locked_by_other_blocked(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns 409 when notebook is locked by another user."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "locked_nb", created_by="test@test.com")
        _seed_lock(tmp_path, "locked_nb", locked_by="other@test.com")
        resp = _client(tmp_path).delete("/api/notebooks/locked_nb")
        assert resp.status_code == 409
        assert "locked by" in resp.json()["message"].lower()


@pytest.mark.unit
class TestLockNotebook:
    """Tests for POST /api/notebooks/{name}/lock."""

    @patch(f"{_P}.start_marimo")
    @patch(f"{_P}.get_marimo_status")
    @patch(f"{_P}.get_project_root")
    def test_acquire_success(
        self,
        mock_root: MagicMock,
        mock_status: MagicMock,
        mock_start: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Acquires lock and returns Marimo URL."""
        mock_root.return_value = tmp_path
        mock_status.return_value = {"running": True, "port": 7805, "pid": 123, "log_file": None}
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "test_nb")
        resp = _client(tmp_path).post("/api/notebooks/test_nb/lock", json={})
        assert resp.status_code == 200
        assert resp.json()["locked"] is True
        assert "test_nb.py" in resp.json()["marimo_url"]

    @patch(f"{_P}.get_project_root")
    def test_conflict(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns 409 when notebook is locked by another user."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "locked_nb")
        _seed_lock(tmp_path, "locked_nb", locked_by="other@test.com")
        resp = _client(tmp_path).post("/api/notebooks/locked_nb/lock", json={})
        assert resp.status_code == 409

    @patch(f"{_P}.get_project_root")
    def test_viewer_forbidden(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Viewer cannot lock notebooks."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        assert (
            _client(tmp_path, role=Role.VIEWER).post("/api/notebooks/t/lock", json={}).status_code
            == 403
        )

    @patch(f"{_P}.get_project_root")
    def test_nonexistent_file_returns_404(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns 404 when notebook file doesn't exist on disk."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        resp = _client(tmp_path).post("/api/notebooks/no_file/lock", json={})
        assert resp.status_code == 404

    @patch(f"{_P}.start_marimo", side_effect=RuntimeError("already running"))
    @patch(f"{_P}.get_marimo_status")
    @patch(f"{_P}.get_project_root")
    def test_marimo_start_race_condition(
        self, mock_root: MagicMock, mock_status: MagicMock, _ms: MagicMock, tmp_path: Path
    ) -> None:
        """Recovers when start_marimo raises RuntimeError (race condition)."""
        mock_root.return_value = tmp_path
        not_running = {"running": False, "port": None, "pid": None, "log_file": None}
        running = {"running": True, "port": 7805, "pid": 123, "log_file": None}
        mock_status.side_effect = [not_running, running]
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "race_nb")
        resp = _client(tmp_path).post("/api/notebooks/race_nb/lock", json={})
        assert resp.status_code == 200
        assert resp.json()["locked"] is True


@pytest.mark.unit
class TestHeartbeat:
    """Tests for POST /api/notebooks/{name}/heartbeat."""

    @patch(f"{_P}.get_project_root")
    def test_refresh_success(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Refreshes lock when held by the requesting user."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "my_nb")
        _seed_lock(tmp_path, "my_nb", locked_by="test@test.com")
        resp = _client(tmp_path).post("/api/notebooks/my_nb/heartbeat", json={})
        assert resp.status_code == 200
        assert resp.json()["refreshed"] is True

    @patch(f"{_P}.get_project_root")
    def test_not_holder(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns 409 when lock is held by a different user."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_lock(tmp_path, "other_nb", locked_by="other@test.com")
        assert (
            _client(tmp_path).post("/api/notebooks/other_nb/heartbeat", json={}).status_code == 409
        )


@pytest.mark.unit
class TestReleaseLock:
    """Tests for POST /api/notebooks/{name}/release."""

    @patch(f"{_P}.get_project_root")
    def test_release_own_lock(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Releases lock when held by the requesting user."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "my_nb")
        _seed_lock(tmp_path, "my_nb", locked_by="test@test.com")
        resp = _client(tmp_path).post("/api/notebooks/my_nb/release", json={})
        assert resp.status_code == 200
        assert resp.json()["released"] is True

    @patch(f"{_P}.get_project_root")
    def test_not_holder(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns 409 when lock is held by a different user."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_lock(tmp_path, "other_nb", locked_by="other@test.com")
        assert _client(tmp_path).post("/api/notebooks/other_nb/release", json={}).status_code == 409


@pytest.mark.unit
class TestForceReleaseLock:
    """Tests for DELETE /api/notebooks/{name}/lock."""

    @patch(f"{_P}._audit")
    @patch(f"{_P}.get_project_root")
    def test_editor_can_force_release(
        self, mock_root: MagicMock, mock_audit: MagicMock, tmp_path: Path
    ) -> None:
        """Editor (has notebooks.manage) can force-release locks and logs audit."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_lock(tmp_path, "locked_nb", locked_by="other@test.com")
        resp = _client(tmp_path, role=Role.EDITOR).delete("/api/notebooks/locked_nb/lock")
        assert resp.status_code == 200
        assert resp.json()["force_released"] is True
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0].value == "notebook_lock_force_released"

    @patch(f"{_P}.get_project_root")
    def test_no_lock_returns_404(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns 404 when no lock exists."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        assert _client(tmp_path).delete("/api/notebooks/no_lock/lock").status_code == 404

    @patch(f"{_P}.get_project_root")
    def test_viewer_forbidden(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Viewer cannot force-release locks."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_lock(tmp_path, "locked_nb", locked_by="other@test.com")
        assert (
            _client(tmp_path, role=Role.VIEWER).delete("/api/notebooks/locked_nb/lock").status_code
            == 403
        )


@pytest.mark.unit
class TestCopyNotebook:
    """Tests for POST /api/notebooks/{name}/copy."""

    @patch(f"{_P}.get_project_root")
    def test_copy_success(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Copies a notebook and returns the copy name."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        _seed_notebook(tmp_path, "original")
        _seed_lock(tmp_path, "original", locked_by="other@test.com")
        resp = _client(tmp_path).post("/api/notebooks/original/copy", json={})
        assert resp.status_code == 201
        assert "original_copy_" in resp.json()["copy_name"]

    @patch(f"{_P}.get_project_root")
    def test_not_found(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Returns 404 when notebook file doesn't exist."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        assert _client(tmp_path).post("/api/notebooks/nonexistent/copy", json={}).status_code == 404

    @patch(f"{_P}.get_project_root")
    def test_viewer_forbidden(self, mock_root: MagicMock, tmp_path: Path) -> None:
        """Viewer cannot copy notebooks."""
        mock_root.return_value = tmp_path
        _init_db(tmp_path)
        assert (
            _client(tmp_path, role=Role.VIEWER).post("/api/notebooks/t/copy", json={}).status_code
            == 403
        )
