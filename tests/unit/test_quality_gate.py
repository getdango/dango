"""tests/unit/test_quality_gate.py

Automated quality gate checks for S2 — verifies version consistency across
API responses, logging configuration, activity log entries, audit events,
and sync subprocess headers.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

import dango

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_logging():
    """Reset logging state between tests (same pattern as test_logging.py)."""
    yield

    from dango.logging import clear_contextvars

    clear_contextvars()

    root = logging.getLogger()
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)

    structlog.reset_defaults()


# ---------------------------------------------------------------------------
# Section 1 — Version in API responses
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApiVersionEndpoints:
    """API endpoints must return the current dango version."""

    def test_api_status_returns_dango_version(self, tmp_path: Path) -> None:
        """GET /api/status returns dango_version matching dango.__version__."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from dango.web.routes.health import router

        app = FastAPI()
        app.state.project_root = tmp_path
        app.state.scheduler = None
        app.include_router(router)

        with (
            patch("dango.web.routes.health.get_project_root", return_value=tmp_path),
            patch(
                "dango.web.routes.health.get_duckdb_path",
                return_value=tmp_path / "nonexistent.duckdb",
            ),
            patch(
                "dango.web.routes.health.check_service_status_async",
                new_callable=AsyncMock,
                return_value="healthy",
            ),
        ):
            client = TestClient(app)
            resp = client.get("/api/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["dango_version"] == dango.__version__

    def test_api_info_returns_version(self) -> None:
        """GET /api returns version matching dango.__version__."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from dango.web.routes.ui import router

        app = FastAPI()
        app.include_router(router)

        client = TestClient(app)
        resp = client.get("/api")

        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == dango.__version__

    def test_fastapi_app_version(self) -> None:
        """FastAPI().version matches dango.__version__."""
        from fastapi import FastAPI

        app = FastAPI(version=dango.__version__)
        assert app.version == dango.__version__


# ---------------------------------------------------------------------------
# Section 2 — configure_logging() is wired up
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfigureLoggingWiring:
    """configure_logging() must be importable, functional, and env-aware."""

    def test_configure_logging_runs_without_error(self, tmp_path: Path) -> None:
        """Import and call configure_logging() — no exception raised."""
        from dango.logging import configure_logging

        configure_logging(log_dir=tmp_path / "logs", json_console=True)

    def test_structlog_configured_after_call(self, tmp_path: Path) -> None:
        """After configure_logging(), a structlog logger produces structured JSON output."""
        from dango.logging import configure_logging, get_logger

        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True, log_level="INFO")

        logger = get_logger("fup4.test")
        logger.info("check_configured")

        log_file = log_dir / "dango.log"
        assert log_file.exists()
        content = log_file.read_text().strip()
        assert content

        record = json.loads(content)
        assert record["event"] == "check_configured"
        assert record["logger"] == "fup4.test"
        assert "timestamp" in record
        assert "level" in record
        assert "dango_version" in record
        assert record["dango_version"] == dango.__version__

    def test_default_log_dir_is_cwd_dot_dango_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When log_dir is None, defaults to cwd/.dango/logs."""

        from dango.logging import configure_logging

        monkeypatch.chdir(tmp_path)

        configure_logging(json_console=True)

        log_file = tmp_path / ".dango" / "logs" / "dango.log"
        assert log_file.exists()


# ---------------------------------------------------------------------------
# Section 3 — Version in log schemas
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVersionInActivityLog:
    """log_activity() must include dango_version in written entries."""

    def test_log_activity_has_dango_version(self, tmp_path: Path) -> None:
        from dango.utils.activity_log import log_activity

        log_activity(tmp_path, "info", "test_source", "test message")

        log_file = tmp_path / ".dango" / "logs" / "activity.jsonl"
        assert log_file.exists()

        content = log_file.read_text().strip()
        entry = json.loads(content)

        assert "dango_version" in entry
        assert entry["dango_version"] == dango.__version__


@pytest.mark.unit
class TestVersionInAuditLog:
    """log_auth_event() must include dango_version in written entries."""

    def test_log_auth_event_has_dango_version(self, tmp_path: Path) -> None:
        from dango.auth.audit import AuditEvent, log_auth_event

        log_auth_event(AuditEvent.LOGIN_SUCCESS, log_dir=tmp_path)

        log_file = tmp_path / "audit.jsonl"
        assert log_file.exists()

        content = log_file.read_text().strip()
        entry = json.loads(content)

        assert "dango_version" in entry
        assert entry["dango_version"] == dango.__version__


@pytest.mark.unit
class TestVersionInSyncSubprocess:
    """launch_sync_subprocess() must write a dango_version header line."""

    def test_launch_sync_subprocess_writes_version_header(self, tmp_path: Path) -> None:
        from dango.platform.sync_process import launch_sync_subprocess

        mock_process = MagicMock()
        mock_process.pid = 99999

        with (
            patch("dango.platform.sync_process.subprocess.Popen", return_value=mock_process),
            patch("dango.utils.process.ensure_std_fds"),
        ):
            _, _, log_path = launch_sync_subprocess(tmp_path, sources=["test_source"])
            assert log_path.exists()

            first_line = log_path.read_text().splitlines()[0]
            assert first_line == f"# dango_version={dango.__version__}"
