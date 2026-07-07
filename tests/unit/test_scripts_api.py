"""tests/unit/test_scripts_api.py

Tests for the scripts API endpoints and page routes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dango.auth.audit import AuditEvent
from dango.auth.models import Role, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_API = "dango.web.routes.scripts_api"
_PATCH_HELPERS = "dango.web.routes.scripts_helpers"
_PATCH_SCRIPTS = "dango.web.routes.scripts"


def _make_admin_user() -> User:
    return User(id="admin-id", email="admin@test.com", role=Role.ADMIN, is_active=True)


def _make_viewer_user() -> User:
    return User(id="viewer-id", email="viewer@test.com", role=Role.VIEWER, is_active=True)


def _make_app(tmp_path: Path, user: User | None = None) -> Any:
    """Build a minimal FastAPI app with the scripts router and injected user."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from dango.exceptions import AuthorizationError
    from dango.web.routes.scripts import router

    app = FastAPI()
    app.state.project_root = tmp_path

    test_user = user or _make_admin_user()

    @app.middleware("http")
    async def inject_user(request: Any, call_next: Any) -> Any:
        request.state.user = test_user
        return await call_next(request)

    @app.exception_handler(AuthorizationError)
    async def auth_error_handler(request: Any, exc: AuthorizationError) -> Any:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    app.include_router(router)
    return app


def _write_script(scripts_dir: Path, name: str, content: str = "print('hello')") -> Path:
    """Create a script file and return its path."""
    scripts_dir.mkdir(parents=True, exist_ok=True)
    file_path = scripts_dir / name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    return file_path


# ---------------------------------------------------------------------------
# TestListScripts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListScripts:
    """Tests for GET /api/scripts."""

    def test_viewer_can_access(self, tmp_path: Path):
        """Viewer role can list scripts."""
        from starlette.testclient import TestClient

        _write_script(tmp_path / "scripts", "hello.py")

        app = _make_app(tmp_path, user=_make_viewer_user())
        client = TestClient(app)

        with patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path):
            resp = client.get("/api/scripts")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TestRunScript
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunScript:
    """Tests for POST /api/scripts/{name}/run."""

    def test_executes_and_captures_stdout(self, tmp_path: Path):
        """Launches script and returns started response with run_id."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        _write_script(tmp_path / "scripts", "hello.py", "print('hello world')")

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.returncode = None
        mock_proc.poll.return_value = None

        helpers._running_processes.clear()

        with (
            patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path),
            patch(f"{_PATCH_API}.subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch(f"{_PATCH_API}.ws_manager") as mock_ws,
            patch(f"{_PATCH_API}.append_log_entry") as mock_log,
        ):
            mock_ws.broadcast = AsyncMock()

            app = _make_app(tmp_path)
            client = TestClient(app)

            resp = client.post(
                "/api/scripts/hello.py/run",
                json={},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "started"
            assert "run_id" in data
            assert data["script_name"] == "hello.py"

            mock_popen.assert_called_once()
            mock_log.assert_called_once()

    def test_non_existent_script_returns_404(self, tmp_path: Path):
        """Returns 404 for non-existent script."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        helpers._running_processes.clear()

        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path):
            resp = client.post(
                "/api/scripts/nonexistent.py/run",
                json={},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert resp.status_code == 404
        assert "DANGO-SC002" in resp.json()["error_code"]

    def test_already_running_returns_409(self, tmp_path: Path):
        """Returns 409 if script is already running."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        _write_script(tmp_path / "scripts", "hello.py")

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None

        helpers._running_processes["hello.py"] = mock_proc

        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path):
            resp = client.post(
                "/api/scripts/hello.py/run",
                json={},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert resp.status_code == 409
        assert "DANGO-SC003" in resp.json()["error_code"]

        helpers._running_processes.clear()

    def test_audit_event_logged(self, tmp_path: Path):
        """Audit event is logged with SCRIPT_RUN type."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        _write_script(tmp_path / "scripts", "hello.py")

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.returncode = None
        mock_proc.poll.return_value = None

        helpers._running_processes.clear()

        with (
            patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path),
            patch(f"{_PATCH_API}.subprocess.Popen", return_value=mock_proc),
            patch(f"{_PATCH_HELPERS}.log_auth_event") as mock_audit,
            patch(f"{_PATCH_API}.ws_manager") as mock_ws,
            patch(f"{_PATCH_API}.append_log_entry"),
        ):
            mock_ws.broadcast = AsyncMock()

            app = _make_app(tmp_path)
            client = TestClient(app)

            client.post(
                "/api/scripts/hello.py/run",
                json={},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args
            assert call_kwargs[0][0] == AuditEvent.SCRIPT_RUN

    def test_viewer_gets_403(self, tmp_path: Path):
        """Viewer role cannot run scripts."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        _write_script(tmp_path / "scripts", "hello.py")
        helpers._running_processes.clear()

        app = _make_app(tmp_path, user=_make_viewer_user())
        client = TestClient(app)

        resp = client.post(
            "/api/scripts/hello.py/run",
            json={},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TestCancelScript
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCancelScript:
    """Tests for POST /api/scripts/{name}/cancel."""

    def test_kills_running_process(self, tmp_path: Path):
        """Cancels a running script process."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        _write_script(tmp_path / "scripts", "long.py")

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = None

        helpers._running_processes["long.py"] = mock_proc

        with (
            patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path),
            patch(f"{_PATCH_API}.ws_manager") as mock_ws,
            patch(f"{_PATCH_API}.append_log_entry"),
        ):
            mock_ws.broadcast = AsyncMock()

            app = _make_app(tmp_path)
            client = TestClient(app)

            resp = client.post(
                "/api/scripts/long.py/cancel",
                json={},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "cancelling"
            mock_proc.terminate.assert_called_once()

        helpers._running_processes.clear()

    def test_not_running_returns_404(self, tmp_path: Path):
        """Returns 404 when no process is running."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        _write_script(tmp_path / "scripts", "hello.py")
        helpers._running_processes.clear()

        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path):
            resp = client.post(
                "/api/scripts/hello.py/cancel",
                json={},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert resp.status_code == 404
        assert "DANGO-SC005" in resp.json()["error_code"]

    def test_audit_event_logged(self, tmp_path: Path):
        """Audit event is logged with SCRIPT_CANCELLED type."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        _write_script(tmp_path / "scripts", "long.py")

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = None

        helpers._running_processes["long.py"] = mock_proc

        with (
            patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path),
            patch(f"{_PATCH_HELPERS}.log_auth_event") as mock_audit,
            patch(f"{_PATCH_API}.ws_manager") as mock_ws,
            patch(f"{_PATCH_API}.append_log_entry"),
        ):
            mock_ws.broadcast = AsyncMock()

            app = _make_app(tmp_path)
            client = TestClient(app)

            client.post(
                "/api/scripts/long.py/cancel",
                json={},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args
            assert call_kwargs[0][0] == AuditEvent.SCRIPT_CANCELLED

        helpers._running_processes.clear()

    def test_viewer_gets_403(self, tmp_path: Path):
        """Viewer role cannot cancel scripts."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        _write_script(tmp_path / "scripts", "hello.py")
        helpers._running_processes.clear()

        app = _make_app(tmp_path, user=_make_viewer_user())
        client = TestClient(app)

        resp = client.post(
            "/api/scripts/hello.py/cancel",
            json={},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TestScriptHistory
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScriptHistory:
    """Tests for GET /api/scripts/{name}/history."""

    def test_empty_history(self, tmp_path: Path):
        """Returns empty items for script with no history."""
        from starlette.testclient import TestClient

        _write_script(tmp_path / "scripts", "hello.py")

        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path):
            resp = client.get("/api/scripts/hello.py/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_returns_entries(self, tmp_path: Path):
        """Returns history entries from JSONL file."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        _write_script(tmp_path / "scripts", "hello.py")

        helpers._append_history(
            tmp_path,
            "hello.py",
            {
                "run_id": "abc-123",
                "script_name": "hello.py",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:05+00:00",
                "duration_seconds": 5.0,
                "status": "success",
                "exit_code": 0,
                "error": None,
            },
        )

        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path):
            resp = client.get("/api/scripts/hello.py/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["run_id"] == "abc-123"
        assert data["items"][0]["status"] == "success"

    def test_respects_limit(self, tmp_path: Path):
        """Respects the limit query parameter."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        _write_script(tmp_path / "scripts", "hello.py")

        for i in range(10):
            helpers._append_history(
                tmp_path,
                "hello.py",
                {
                    "run_id": f"run-{i}",
                    "script_name": "hello.py",
                    "started_at": f"2026-01-01T00:00:{i:02d}+00:00",
                    "finished_at": f"2026-01-01T00:00:{i + 5:02d}+00:00",
                    "duration_seconds": 5.0,
                    "status": "success",
                    "exit_code": 0,
                    "error": None,
                },
            )

        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path):
            resp = client.get("/api/scripts/hello.py/history?limit=3")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 3
        assert data["total"] == 10

    def test_non_existent_script(self, tmp_path: Path):
        """Returns 404 for script that doesn't exist."""
        from starlette.testclient import TestClient

        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path):
            resp = client.get("/api/scripts/nonexistent.py/history")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestScriptLogPage
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScriptLogPage:
    """Tests for GET /scripts/{name}/logs/{run_id}."""

    def test_renders_with_content(self, tmp_path: Path):
        """Renders log page with stdout/stderr content."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        _write_script(tmp_path / "scripts", "hello.py")

        log_dir = helpers._get_log_dir(tmp_path) / "test-run-id"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "stdout.txt").write_text("hello world\n")
        (log_dir / "stderr.txt").write_text("warning: something\n")
        (log_dir / "meta.json").write_text(
            json.dumps(
                {
                    "run_id": "test-run-id",
                    "script_name": "hello.py",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "finished_at": "2026-01-01T00:00:05+00:00",
                    "duration_seconds": 5.0,
                    "status": "success",
                    "exit_code": 0,
                    "error": None,
                }
            )
        )

        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_SCRIPTS}.get_project_root", return_value=tmp_path):
            resp = client.get("/scripts/hello.py/logs/test-run-id")
        assert resp.status_code == 200
        assert "hello world" in resp.text
        assert "warning: something" in resp.text
        assert "test-run-id" in resp.text

    def test_non_existent_run_returns_404(self, tmp_path: Path):
        """Returns 404 page when run doesn't exist."""
        from starlette.testclient import TestClient

        _write_script(tmp_path / "scripts", "hello.py")

        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_SCRIPTS}.get_project_root", return_value=tmp_path):
            resp = client.get("/scripts/hello.py/logs/nonexistent-run")
        assert resp.status_code == 404
        assert "not found" in resp.text.lower()

    def test_non_existent_script_returns_404(self, tmp_path: Path):
        """Returns 404 page when script doesn't exist."""
        from starlette.testclient import TestClient

        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_SCRIPTS}.get_project_root", return_value=tmp_path):
            resp = client.get("/scripts/nonexistent.py/logs/some-run")
        assert resp.status_code == 404
        assert "not found" in resp.text.lower()

    def test_handles_empty_stdout_stderr(self, tmp_path: Path):
        """Renders gracefully when stdout/stderr are empty."""
        from starlette.testclient import TestClient

        import dango.web.routes.scripts_helpers as helpers

        _write_script(tmp_path / "scripts", "hello.py")

        log_dir = helpers._get_log_dir(tmp_path) / "empty-run"
        log_dir.mkdir(parents=True, exist_ok=True)

        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch(f"{_PATCH_SCRIPTS}.get_project_root", return_value=tmp_path):
            resp = client.get("/scripts/hello.py/logs/empty-run")
        assert resp.status_code == 200
        assert "(empty)" in resp.text


# ---------------------------------------------------------------------------
# TestNavBar
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNavBar:
    """Tests that base.html contains Scripts nav link."""

    def test_base_html_contains_scripts_link(self):
        """Base template contains Scripts nav link."""
        from pathlib import Path

        base_path = (
            Path(__file__).parent.parent.parent / "dango" / "web" / "templates" / "base.html"
        )
        content = base_path.read_text()
        assert 'href="/scripts"' in content
        assert "Scripts" in content
