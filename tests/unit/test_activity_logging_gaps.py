"""tests/unit/test_activity_logging_gaps.py

Tests that activity logging (append_log_entry) is called for CSV uploads
and schedule manual triggers (P4-2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from dango.auth.models import Role, User

_UPLOAD_PATCH = "dango.web.routes.upload"
_SCHED_PATCH = "dango.web.routes.schedules"


def _admin_user() -> User:
    return User(id="admin-id", email="admin@test.com", role=Role.ADMIN, is_active=True)


# ---------------------------------------------------------------------------
# Helpers — upload app
# ---------------------------------------------------------------------------


def _make_upload_app(tmp_path: Path) -> Any:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from dango.exceptions import AuthorizationError
    from dango.web.routes.upload import router

    app = FastAPI()
    app.state.project_root = tmp_path
    app.include_router(router)

    user = _admin_user()

    @app.middleware("http")
    async def inject_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        return await call_next(request)

    @app.exception_handler(AuthorizationError)
    async def auth_err(request: Any, exc: AuthorizationError) -> Any:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    return app


def _setup_csv_source(tmp_path: Path) -> Path:
    """Create a minimal CSV source config and data directory."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir(parents=True, exist_ok=True)
    sources = {
        "sources": [
            {"name": "test_csv", "type": "csv", "csv": {"directory": str(tmp_path / "data")}}
        ]
    }
    with open(dango_dir / "sources.yml", "w") as f:
        yaml.dump(sources, f)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


# ---------------------------------------------------------------------------
# Helpers — schedule app
# ---------------------------------------------------------------------------


def _make_schedule_app(tmp_path: Path, scheduler: Any = None) -> Any:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from dango.exceptions import AuthorizationError
    from dango.web.routes.schedules import router

    app = FastAPI()
    app.state.project_root = tmp_path
    if scheduler is not None:
        app.state.scheduler = scheduler
    app.include_router(router)

    user = _admin_user()

    @app.middleware("http")
    async def inject_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        return await call_next(request)

    @app.exception_handler(AuthorizationError)
    async def auth_err(request: Any, exc: AuthorizationError) -> Any:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    return app


# ---------------------------------------------------------------------------
# Tests — CSV upload activity logging
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUploadActivityLogging:
    """CSV upload should call append_log_entry with level=success."""

    def test_upload_logs_activity(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        _setup_csv_source(tmp_path)
        app = _make_upload_app(tmp_path)

        with (
            patch(f"{_UPLOAD_PATCH}.get_project_root", return_value=tmp_path),
            patch(f"{_UPLOAD_PATCH}.append_log_entry") as mock_log,
            patch(
                f"{_UPLOAD_PATCH}.ws_manager", new_callable=lambda: MagicMock(broadcast=AsyncMock())
            ),
            patch(f"{_UPLOAD_PATCH}.log_auth_event"),
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/sources/test_csv/upload-csv",
                files={"file": ("test.csv", b"a,b\n1,2\n", "text/csv")},
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 200
        assert mock_log.called
        log_entry = mock_log.call_args[0][0]
        assert log_entry["level"] == "success"
        assert log_entry["source"] == "test_csv"
        assert "CSV uploaded" in log_entry["message"]


# ---------------------------------------------------------------------------
# Tests — Schedule trigger activity logging
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScheduleTriggerActivityLogging:
    """Schedule manual trigger should call append_log_entry."""

    def test_trigger_logs_activity(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        # Write schedule config
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir(parents=True, exist_ok=True)
        schedules = {
            "schedules": [
                {
                    "name": "nightly",
                    "type": "sync",
                    "cron": "0 2 * * *",
                    "sources": ["stripe"],
                    "enabled": True,
                }
            ]
        }
        with open(dango_dir / "schedules.yml", "w") as f:
            yaml.dump(schedules, f)

        scheduler = MagicMock()
        scheduler.get_jobs.return_value = []
        mock_job = MagicMock()
        mock_job.id = "manual:nightly:test"
        scheduler.add_job.return_value = mock_job

        app = _make_schedule_app(tmp_path, scheduler=scheduler)

        with (
            patch(f"{_SCHED_PATCH}.get_project_root", return_value=tmp_path),
            patch(f"{_SCHED_PATCH}.append_log_entry") as mock_log,
            patch(f"{_SCHED_PATCH}.log_auth_event"),
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/schedules/nightly/trigger",
                headers={"X-Requested-With": "fetch"},
            )

        assert resp.status_code == 200
        assert mock_log.called
        log_entry = mock_log.call_args[0][0]
        assert log_entry["level"] == "info"
        assert log_entry["source"] == "schedule:nightly"
        assert "manually triggered" in log_entry["message"]
