"""tests/unit/test_scripts_helpers.py

Tests for script discovery, validation, history helpers, page routes, and navbar.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dango.auth.models import Role, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_HELPERS = "dango.web.routes.scripts_helpers"
_PATCH_SCRIPTS = "dango.web.routes.scripts"


def _make_admin_user() -> User:
    return User(id="admin-id", email="admin@test.com", role=Role.ADMIN, is_active=True)


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
# TestDiscoverScripts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiscoverScripts:
    """Tests for _discover_scripts()."""

    def test_empty_dir(self, tmp_path: Path):
        """Returns empty list when no scripts exist."""
        from dango.web.routes.scripts_helpers import _discover_scripts

        result = _discover_scripts(tmp_path)
        assert result == []

    def test_discovers_py_files(self, tmp_path: Path):
        """Discovers .py files in scripts/ directory."""
        from dango.web.routes.scripts_helpers import _discover_scripts

        _write_script(tmp_path / "scripts", "hello.py")
        _write_script(tmp_path / "scripts", "utils.py")

        result = _discover_scripts(tmp_path)
        names = [s["name"] for s in result]
        assert "hello.py" in names
        assert "utils.py" in names

    def test_skips_init_py(self, tmp_path: Path):
        """Skips __init__.py files."""
        from dango.web.routes.scripts_helpers import _discover_scripts

        _write_script(tmp_path / "scripts", "__init__.py")
        _write_script(tmp_path / "scripts", "main.py")

        result = _discover_scripts(tmp_path)
        names = [s["name"] for s in result]
        assert "__init__.py" not in names
        assert "main.py" in names

    def test_skips_dotfiles(self, tmp_path: Path):
        """Skips dot-prefixed files."""
        from dango.web.routes.scripts_helpers import _discover_scripts

        _write_script(tmp_path / "scripts", ".hidden.py")
        _write_script(tmp_path / "scripts", "visible.py")

        result = _discover_scripts(tmp_path)
        names = [s["name"] for s in result]
        assert ".hidden.py" not in names
        assert "visible.py" in names

    def test_skips_underscore_prefixed(self, tmp_path: Path):
        """Skips _-prefixed files and directories."""
        from dango.web.routes.scripts_helpers import _discover_scripts

        _write_script(tmp_path / "scripts", "_internal.py")
        _write_script(tmp_path / "scripts", "public.py")
        _write_script(tmp_path / "scripts" / "_private", "nested.py")

        result = _discover_scripts(tmp_path)
        names = [s["name"] for s in result]
        assert "_internal.py" not in names
        assert "public.py" in names
        assert "nested.py" not in names


@pytest.mark.unit
class TestDiscoverScriptsTimeout:
    """1.0.8-BUGS-FOUND: Scripts previously had no configurable timeout at all."""

    def test_default_timeout_when_no_config(self, tmp_path: Path):
        from dango.config.scripts import DEFAULT_SCRIPT_TIMEOUT_SECONDS
        from dango.web.routes.scripts_helpers import _discover_scripts

        _write_script(tmp_path / "scripts", "quick.py")

        result = _discover_scripts(tmp_path)
        assert result[0]["timeout_seconds"] == DEFAULT_SCRIPT_TIMEOUT_SECONDS

    def test_configured_timeout_override(self, tmp_path: Path):
        from dango.web.routes.scripts_helpers import _discover_scripts, _get_script_timeout

        _write_script(tmp_path / "scripts", "orchestrator_v2.py")
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "scripts.yml").write_text(
            "scripts:\n  - path: orchestrator_v2.py\n    timeout_seconds: 1800\n"
        )

        result = _discover_scripts(tmp_path)
        assert result[0]["timeout_seconds"] == 1800
        assert _get_script_timeout(tmp_path, "orchestrator_v2.py") == 1800

    def test_recursive_discovery(self, tmp_path: Path):
        """Recursively discovers scripts in subdirectories."""
        from dango.web.routes.scripts_helpers import _discover_scripts

        _write_script(tmp_path / "scripts" / "marketing", "report.py")
        _write_script(tmp_path / "scripts" / "ops", "cleanup.py")

        result = _discover_scripts(tmp_path)
        names = [s["name"] for s in result]
        assert "marketing/report.py" in names
        assert "ops/cleanup.py" in names


# ---------------------------------------------------------------------------
# TestValidateScriptPath
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidateScriptPath:
    """Tests for _validate_script_path()."""

    def test_path_traversal_blocked(self, tmp_path: Path):
        """Path traversal outside scripts/ returns 400 JSONResponse."""
        from dango.web.routes.scripts_helpers import _validate_script_path

        result = _validate_script_path(tmp_path, "../outside.txt")
        assert hasattr(result, "status_code")
        assert result.status_code == 400  # type: ignore[union-attr]
        assert "DANGO-SC001" in result.body.decode()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TestSafeFilename
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSafeFilename:
    """Tests for _safe_filename()."""

    def test_replaces_slashes(self):
        """Replaces / and \\ with __."""
        from dango.web.routes.scripts_helpers import _safe_filename

        assert _safe_filename("marketing/report.py") == "marketing__report.py"
        assert _safe_filename("simple.py") == "simple.py"


# ---------------------------------------------------------------------------
# TestHistoryHelpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHistoryHelpers:
    """Tests for _append_history() and _load_history()."""

    def test_append_and_load(self, tmp_path: Path):
        """Append a history entry and load it back."""
        from dango.web.routes.scripts_helpers import _append_history, _load_history

        entry = {
            "run_id": "abc-123",
            "script_name": "hello.py",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:05+00:00",
            "duration_seconds": 5.0,
            "status": "success",
            "exit_code": 0,
            "error": None,
        }
        _append_history(tmp_path, "hello.py", entry)

        result = _load_history(tmp_path, "hello.py", limit=50)
        assert len(result) == 1
        assert result[0]["run_id"] == "abc-123"
        assert result[0]["status"] == "success"

    def test_load_respects_limit(self, tmp_path: Path):
        """Only returns up to limit entries."""
        from dango.web.routes.scripts_helpers import _append_history, _load_history

        for i in range(10):
            _append_history(
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

        result = _load_history(tmp_path, "hello.py", limit=3)
        assert len(result) == 3

    def test_load_limit_zero_returns_all(self, tmp_path: Path):
        """limit=0 means no limit (for total counting)."""
        from dango.web.routes.scripts_helpers import _append_history, _load_history

        for i in range(5):
            _append_history(
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

        result = _load_history(tmp_path, "hello.py", limit=0)
        assert len(result) == 5

    def test_empty_history(self, tmp_path: Path):
        """Returns empty list for script with no history."""
        from dango.web.routes.scripts_helpers import _load_history

        result = _load_history(tmp_path, "nonexistent.py")
        assert result == []


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
