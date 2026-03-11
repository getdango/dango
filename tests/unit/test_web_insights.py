"""tests/unit/test_web_insights.py

Tests for dango/web/routes/insights.py — insights API endpoints.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.exceptions import (
    AuthenticationError,
    AuthorizationError,
    DangoError,
    ValidationError,
)
from dango.web.routes.insights import router

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _make_user(role: Role = Role.ADMIN) -> User:
    """Create a test user."""
    return User(
        id="u-test-1",
        email="test@test.com",
        password_hash="hashed",
        role=role,
        is_active=True,
    )


def _make_app(project_root: Path) -> FastAPI:
    """Create a minimal FastAPI app with the insights router."""
    app = FastAPI()
    app.state.project_root = project_root

    status_map: dict[type[DangoError], int] = {
        AuthenticationError: 401,
        AuthorizationError: 403,
        ValidationError: 400,
        DangoError: 500,
    }

    @app.exception_handler(DangoError)
    async def dango_error_handler(
        request: Request,
        exc: DangoError,
    ) -> JSONResponse:
        status_code = 500
        for cls in type(exc).__mro__:
            if cls in status_map:
                status_code = status_map[cls]
                break
        return JSONResponse(
            status_code=status_code,
            content={"error_code": exc.error_code, "message": exc.user_message},
        )

    app.include_router(router)
    return app


def _setup_client(
    tmp_path: Path,
    role: Role = Role.ADMIN,
) -> tuple[TestClient, Path]:
    """Create a test client with auth middleware injecting a user."""
    user = _make_user(role)
    app = _make_app(tmp_path)

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    client = TestClient(app, raise_server_exceptions=False)
    return client, tmp_path


def _make_mock_result(
    name: str = "revenue",
    value: float = 100.0,
    change_pct: float = 5.0,
    exceeds_threshold: bool = False,
) -> MagicMock:
    """Build a mock AnalysisResult."""
    mock = MagicMock()
    mock.metric.metric_name = name
    mock.metric.value = value
    mock.metric.error = None
    mock.metric.source = "raw_stripe"
    mock.metric.table_name = "payments"
    mock.comparison.change_pct = change_pct
    mock.comparison.comparison_type.value = "week_over_week"
    mock.comparison.baseline_value = 80.0
    mock.comparison.exceeds_threshold = exceeds_threshold
    mock.comparison.trend_direction = None
    mock.comparison.trend_slope = None
    mock.comparison.forecast_threshold_days = None
    mock.comparison.current_value = value
    mock.drill_down = []
    return mock


# ---------------------------------------------------------------------------
# GET /api/insights
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetInsights:
    """Tests for GET /api/insights."""

    @patch("dango.web.routes.insights.get_project_root")
    @patch("dango.analysis.metrics.run_analysis")
    @patch("dango.web.routes.insights.log_auth_event")
    def test_returns_insights(
        self,
        mock_audit: MagicMock,
        mock_run: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """GET /api/insights returns categorised metrics."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root
        mock_run.return_value = [_make_mock_result()]

        resp = client.get("/api/insights")
        assert resp.status_code == 200
        data = resp.json()
        assert "metrics" in data
        assert data["total"] == 1
        assert data["flagged"] == 0

    @patch("dango.web.routes.insights.get_project_root")
    @patch("dango.analysis.metrics.run_analysis")
    @patch("dango.web.routes.insights.log_auth_event")
    def test_empty_results(
        self,
        mock_audit: MagicMock,
        mock_run: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """GET /api/insights with no metrics returns empty list."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root
        mock_run.return_value = []

        resp = client.get("/api/insights")
        assert resp.status_code == 200
        data = resp.json()
        assert data["metrics"] == []
        assert data["total"] == 0

    @patch("dango.web.routes.insights.get_project_root")
    @patch("dango.analysis.metrics.run_analysis")
    @patch("dango.web.routes.insights.log_auth_event")
    def test_audit_logged(
        self,
        mock_audit: MagicMock,
        mock_run: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """GET /api/insights logs audit event."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root
        mock_run.return_value = []

        client.get("/api/insights")
        mock_audit.assert_called_once()


# ---------------------------------------------------------------------------
# POST /api/insights/run
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunInsights:
    """Tests for POST /api/insights/run."""

    @patch("dango.web.routes.insights.get_project_root")
    @patch("dango.analysis.metrics.run_analysis")
    @patch("dango.web.routes.insights.log_auth_event")
    def test_run_returns_fresh_results(
        self,
        mock_audit: MagicMock,
        mock_run: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """POST /api/insights/run executes and returns results."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root
        mock_run.return_value = [
            _make_mock_result(exceeds_threshold=True),
        ]

        resp = client.post(
            "/api/insights/run",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["flagged"] == 1

    @patch("dango.web.routes.insights.get_project_root")
    @patch("dango.analysis.metrics.run_analysis")
    @patch("dango.web.routes.insights.log_auth_event")
    def test_run_with_source_filter(
        self,
        mock_audit: MagicMock,
        mock_run: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """POST /api/insights/run?source=stripe passes source filter."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root
        mock_run.return_value = []

        resp = client.post(
            "/api/insights/run?source=stripe",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs.get("source_filter") == ["raw_stripe"]


# ---------------------------------------------------------------------------
# GET /api/insights/history
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetMetricHistory:
    """Tests for GET /api/insights/history."""

    @patch("dango.web.routes.insights.get_project_root")
    @patch("dango.utils.dango_db.connect")
    @patch("dango.web.routes.insights.log_auth_event")
    def test_returns_history(
        self,
        mock_audit: MagicMock,
        mock_connect: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """GET /api/insights/history returns data points."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            (100.0, "2026-03-10T12:00:00"),
            (95.0, "2026-03-09T12:00:00"),
        ]

        resp = client.get("/api/insights/history?metric=test_metric&days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert data["metric"] == "test_metric"
        assert data["days"] == 7
        assert len(data["data_points"]) == 2
