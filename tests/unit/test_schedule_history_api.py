"""tests/unit/test_schedule_history_api.py

Tests for the schedule execution history API endpoints in
dango/web/routes/schedules.py.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers (shared with test_execution_history.py)
# ---------------------------------------------------------------------------


def _load_migration():
    """Load the migration module (filename starts with digit, can't import directly)."""
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "dango"
        / "migrations"
        / "scheduler"
        / "001_execution_history.py"
    )
    spec = importlib.util.spec_from_file_location("migration_001", migration_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _create_db(tmp_path: Path) -> Path:
    """Create a scheduler DB with the execution_history table."""
    migration = _load_migration()
    db_path = tmp_path / "scheduler.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    migration.upgrade(conn)
    conn.commit()
    conn.close()
    return db_path


def _insert_record(
    db_path: Path,
    schedule_name: str = "daily-sync",
    status: str = "success",
    started_at: str | None = None,
    duration: float | None = 10.0,
) -> int:
    """Insert a record directly for test setup."""
    if started_at is None:
        started_at = datetime.now(tz=timezone.utc).isoformat()
    ended_at = datetime.now(tz=timezone.utc).isoformat() if status != "running" else None

    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute(
        """
        INSERT INTO execution_history
        (schedule_name, started_at, ended_at, status, duration_seconds)
        VALUES (?, ?, ?, ?, ?)
        """,
        (schedule_name, started_at, ended_at, status, duration),
    )
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id


def _make_test_user():
    """Create a fake admin user for route tests."""
    from dango.auth.models import Role, User

    return User(
        id="test-user-id",
        email="admin@test.com",
        role=Role.ADMIN,
        is_active=True,
    )


def _make_schedule_app(tmp_path: Path) -> Any:
    """Build a minimal FastAPI app with schedules routes and an injected user."""
    from fastapi import FastAPI

    from dango.web.routes.schedules import router

    app = FastAPI()
    app.state.project_root = tmp_path
    app.include_router(router)

    test_user = _make_test_user()

    @app.middleware("http")
    async def inject_user(request: Any, call_next: Any) -> Any:
        request.state.user = test_user
        return await call_next(request)

    return app


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScheduleHistoryAPI:
    """Test the /api/schedules/{name}/history endpoint."""

    def test_returns_history_data(self, tmp_path):
        from fastapi.testclient import TestClient

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        db_path = _create_db(tmp_path)
        db_path.rename(dango_dir / "scheduler.db")

        app = _make_schedule_app(tmp_path)

        with patch("dango.web.routes.schedules.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules/daily-sync/history")

        assert resp.status_code == 200
        data = resp.json()
        assert data["schedule_name"] == "daily-sync"
        assert "items" in data
        assert "total" in data

    def test_validates_status_param(self, tmp_path):
        from fastapi.testclient import TestClient

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        db_path = _create_db(tmp_path)
        db_path.rename(dango_dir / "scheduler.db")

        app = _make_schedule_app(tmp_path)

        with patch("dango.web.routes.schedules.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules/daily-sync/history?status=invalid")

        assert resp.status_code == 400
        assert "Invalid status" in resp.json()["message"]

    def test_validates_since_param(self, tmp_path):
        from fastapi.testclient import TestClient

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        db_path = _create_db(tmp_path)
        db_path.rename(dango_dir / "scheduler.db")

        app = _make_schedule_app(tmp_path)

        with patch("dango.web.routes.schedules.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules/daily-sync/history?since=banana")

        assert resp.status_code == 400
        assert "since" in resp.json()["message"]

    def test_validates_until_param(self, tmp_path):
        from fastapi.testclient import TestClient

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        db_path = _create_db(tmp_path)
        db_path.rename(dango_dir / "scheduler.db")

        app = _make_schedule_app(tmp_path)

        with patch("dango.web.routes.schedules.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules/daily-sync/history?until=not-a-date")

        assert resp.status_code == 400
        assert "until" in resp.json()["message"]

    def test_accepts_valid_iso_since(self, tmp_path):
        from fastapi.testclient import TestClient

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        db_path = _create_db(tmp_path)
        db_path.rename(dango_dir / "scheduler.db")

        app = _make_schedule_app(tmp_path)

        with patch("dango.web.routes.schedules.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules/daily-sync/history?since=2026-01-01T00:00:00Z")

        assert resp.status_code == 200


@pytest.mark.unit
class TestRecentExecutionsAPI:
    """Test the /api/schedules/history/recent endpoint."""

    def test_returns_recent_history(self, tmp_path):
        from fastapi.testclient import TestClient

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        db_path = _create_db(tmp_path)
        _insert_record(db_path, schedule_name="test-sched")
        db_path.rename(dango_dir / "scheduler.db")

        app = _make_schedule_app(tmp_path)

        with patch("dango.web.routes.schedules.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules/history/recent")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    def test_respects_limit(self, tmp_path):
        from fastapi.testclient import TestClient

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        db_path = _create_db(tmp_path)
        for _ in range(5):
            _insert_record(db_path, schedule_name="test-sched")
        db_path.rename(dango_dir / "scheduler.db")

        app = _make_schedule_app(tmp_path)

        with patch("dango.web.routes.schedules.get_project_root", return_value=tmp_path):
            client = TestClient(app)
            resp = client.get("/api/schedules/history/recent?limit=2")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
