"""tests/unit/test_web_ai.py

Tests for dango.web.routes.ai — catalog summary endpoint.
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
from dango.web.routes.ai import router

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
    """Create a minimal FastAPI app with the AI router."""
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


def _setup_unauthenticated_client(tmp_path: Path) -> TestClient:
    """Create a test client with no user set (unauthenticated)."""
    app = _make_app(tmp_path)
    return TestClient(app, raise_server_exceptions=False)


def _mock_source_config(name: str = "stripe", source_type: str = "stripe") -> dict[str, Any]:
    """Build a mock source config dict."""
    return {"name": name, "type": source_type, "enabled": True}


def _mock_freshness() -> dict[str, Any]:
    """Build a mock freshness dict."""
    return {
        "status": "synced",
        "hours_since_sync": 2.0,
        "last_sync_time": "2026-03-25T10:00:00",
        "last_sync_status": "success",
    }


def _mock_tables_info() -> dict[str, Any]:
    """Build a mock tables info dict."""
    return {
        "total_rows": 1500,
        "tables": [
            {"name": "payments", "row_count": 1000, "schema": "raw_stripe"},
            {"name": "customers", "row_count": 500, "schema": "raw_stripe"},
        ],
        "has_multiple_tables": True,
    }


def _mock_dbt_model(name: str = "stg_payments") -> dict[str, Any]:
    """Build a mock dbt model dict."""
    return {
        "name": name,
        "unique_id": f"model.dango.{name}",
        "path": f"models/staging/{name}.sql",
        "materialization": "view",
        "schema": "staging",
        "database": "warehouse",
        "depends_on": ["source.dango.stripe.payments"],
        "description": f"Staged {name} data",
        "tags": ["staging"],
        "row_count": 1000,
        "last_run": "2026-03-25T09:00:00",
        "status": "success",
    }


# ---------------------------------------------------------------------------
# GET /api/catalog/summary
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetCatalogSummary:
    """Tests for GET /api/catalog/summary."""

    @patch("dango.web.routes.ai.get_duckdb_path")
    @patch("dango.web.routes.ai.get_dbt_models")
    @patch("dango.web.routes.ai.load_sources_config")
    @patch("dango.web.routes.ai.get_project_root")
    @patch("dango.web.routes.ai.log_auth_event")
    def test_empty_project(
        self,
        mock_audit: MagicMock,
        mock_root: MagicMock,
        mock_sources: MagicMock,
        mock_dbt: MagicMock,
        mock_db_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Empty project returns 200 with empty lists."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root
        mock_sources.return_value = []
        mock_dbt.return_value = []
        mock_db_path.return_value = tmp_path / "data" / "warehouse.duckdb"

        resp = client.get("/api/catalog/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sources"] == []
        assert data["dbt_models"] == []
        assert data["totals"]["source_count"] == 0
        assert data["totals"]["table_count"] == 0
        assert data["totals"]["model_count"] == 0

    @patch("dango.web.routes.ai._get_column_schema")
    @patch("dango.web.routes.ai.get_duckdb_path")
    @patch("dango.web.routes.ai.get_source_tables_info")
    @patch("dango.web.routes.ai.get_source_freshness")
    @patch("dango.web.routes.ai.get_dbt_models")
    @patch("dango.web.routes.ai.load_sources_config")
    @patch("dango.web.routes.ai.get_project_root")
    @patch("dango.web.routes.ai.log_auth_event")
    def test_source_with_tables(
        self,
        mock_audit: MagicMock,
        mock_root: MagicMock,
        mock_sources: MagicMock,
        mock_dbt: MagicMock,
        mock_freshness: MagicMock,
        mock_tables: MagicMock,
        mock_db_path: MagicMock,
        mock_col_schema: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Source with tables returns correct response shape."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root
        mock_sources.return_value = [_mock_source_config()]
        mock_dbt.return_value = []
        mock_freshness.return_value = _mock_freshness()
        mock_tables.return_value = _mock_tables_info()
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.touch()
        mock_db_path.return_value = db_path
        mock_col_schema.return_value = [
            {"name": "id", "type": "INTEGER", "nullable": False},
            {"name": "amount", "type": "DOUBLE", "nullable": True},
        ]

        resp = client.get("/api/catalog/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["sources"]) == 1
        src = data["sources"][0]
        assert src["name"] == "stripe"
        assert src["type"] == "stripe"
        assert src["status"] == "synced"
        assert src["row_count"] == 1500
        assert len(src["tables"]) == 2
        assert src["tables"][0]["name"] == "payments"
        assert len(src["tables"][0]["columns"]) == 2
        assert src["tables"][0]["columns"][0]["name"] == "id"
        assert data["totals"]["table_count"] == 2

    @patch("dango.web.routes.ai.get_duckdb_path")
    @patch("dango.web.routes.ai.get_dbt_models")
    @patch("dango.web.routes.ai.load_sources_config")
    @patch("dango.web.routes.ai.get_project_root")
    @patch("dango.web.routes.ai.log_auth_event")
    def test_dbt_models_included(
        self,
        mock_audit: MagicMock,
        mock_root: MagicMock,
        mock_sources: MagicMock,
        mock_dbt: MagicMock,
        mock_db_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        """dbt models included with correct fields."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root
        mock_sources.return_value = []
        mock_dbt.return_value = [_mock_dbt_model()]
        mock_db_path.return_value = tmp_path / "data" / "warehouse.duckdb"

        resp = client.get("/api/catalog/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["dbt_models"]) == 1
        model = data["dbt_models"][0]
        assert model["name"] == "stg_payments"
        assert model["unique_id"] == "model.dango.stg_payments"
        assert model["materialization"] == "view"
        assert model["schema_name"] == "staging"
        assert model["depends_on"] == ["source.dango.stripe.payments"]
        assert model["tags"] == ["staging"]
        assert data["totals"]["model_count"] == 1

    @patch("dango.governance.pii_detector.get_pii_findings")
    @patch("dango.governance.schema_drift.get_drift_history")
    @patch("dango.web.routes.ai.get_duckdb_path")
    @patch("dango.web.routes.ai.get_source_tables_info")
    @patch("dango.web.routes.ai.get_source_freshness")
    @patch("dango.web.routes.ai.get_dbt_models")
    @patch("dango.web.routes.ai.load_sources_config")
    @patch("dango.web.routes.ai.get_project_root")
    @patch("dango.web.routes.ai.log_auth_event")
    def test_quality_signals_for_admin(
        self,
        mock_audit: MagicMock,
        mock_root: MagicMock,
        mock_sources: MagicMock,
        mock_dbt: MagicMock,
        mock_freshness: MagicMock,
        mock_tables: MagicMock,
        mock_db_path: MagicMock,
        mock_drift: MagicMock,
        mock_pii: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Admin (has governance.view) gets populated quality signals."""
        client, project_root = _setup_client(tmp_path, role=Role.ADMIN)
        mock_root.return_value = project_root
        mock_sources.return_value = [_mock_source_config()]
        mock_dbt.return_value = []
        mock_freshness.return_value = _mock_freshness()
        mock_tables.return_value = {"total_rows": 100, "tables": [], "has_multiple_tables": False}
        mock_db_path.return_value = tmp_path / "data" / "warehouse.duckdb"
        mock_drift.return_value = [{"id": 1}, {"id": 2}]
        mock_pii.return_value = [{"id": 1}]

        resp = client.get("/api/catalog/summary")
        assert resp.status_code == 200
        quality = resp.json()["sources"][0]["quality"]
        assert quality["drift_event_count"] == 2
        assert quality["pii_column_count"] == 1

    @patch("dango.web.routes.ai.get_duckdb_path")
    @patch("dango.web.routes.ai.get_source_tables_info")
    @patch("dango.web.routes.ai.get_source_freshness")
    @patch("dango.web.routes.ai.get_dbt_models")
    @patch("dango.web.routes.ai.load_sources_config")
    @patch("dango.web.routes.ai.get_project_root")
    @patch("dango.web.routes.ai.log_auth_event")
    def test_quality_signals_zeroed_for_editor(
        self,
        mock_audit: MagicMock,
        mock_root: MagicMock,
        mock_sources: MagicMock,
        mock_dbt: MagicMock,
        mock_freshness: MagicMock,
        mock_tables: MagicMock,
        mock_db_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Editor (no governance.view) gets zeroed quality signals."""
        client, project_root = _setup_client(tmp_path, role=Role.EDITOR)
        mock_root.return_value = project_root
        mock_sources.return_value = [_mock_source_config()]
        mock_dbt.return_value = []
        mock_freshness.return_value = _mock_freshness()
        mock_tables.return_value = {"total_rows": 100, "tables": [], "has_multiple_tables": False}
        mock_db_path.return_value = tmp_path / "data" / "warehouse.duckdb"

        resp = client.get("/api/catalog/summary")
        assert resp.status_code == 200
        quality = resp.json()["sources"][0]["quality"]
        assert quality["drift_event_count"] == 0
        assert quality["pii_column_count"] == 0

    @patch("dango.web.routes.ai.get_duckdb_path")
    @patch("dango.web.routes.ai.get_dbt_models")
    @patch("dango.web.routes.ai.load_sources_config")
    @patch("dango.web.routes.ai.get_project_root")
    @patch("dango.web.routes.ai.log_auth_event")
    def test_audit_event_logged(
        self,
        mock_audit: MagicMock,
        mock_root: MagicMock,
        mock_sources: MagicMock,
        mock_dbt: MagicMock,
        mock_db_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Audit event is logged on catalog summary access."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root
        mock_sources.return_value = []
        mock_dbt.return_value = []
        mock_db_path.return_value = tmp_path / "data" / "warehouse.duckdb"

        client.get("/api/catalog/summary")
        mock_audit.assert_called_once()
        from dango.auth.audit import AuditEvent

        assert mock_audit.call_args[0][0] == AuditEvent.AI_CATALOG_VIEWED

    def test_unauthenticated_returns_401(self, tmp_path: Path) -> None:
        """Unauthenticated request returns 401."""
        client = _setup_unauthenticated_client(tmp_path)
        resp = client.get("/api/catalog/summary")
        assert resp.status_code == 401

    @patch("dango.web.routes.ai.get_duckdb_path")
    @patch("dango.web.routes.ai.get_dbt_models")
    @patch("dango.web.routes.ai.load_sources_config")
    @patch("dango.web.routes.ai.get_project_root")
    @patch("dango.web.routes.ai.log_auth_event")
    def test_has_description_field(
        self,
        mock_audit: MagicMock,
        mock_root: MagicMock,
        mock_sources: MagicMock,
        mock_dbt: MagicMock,
        mock_db_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Response has self-describing description field."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root
        mock_sources.return_value = []
        mock_dbt.return_value = []
        mock_db_path.return_value = tmp_path / "data" / "warehouse.duckdb"

        resp = client.get("/api/catalog/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "description" in data
        assert len(data["description"]) > 0

    @patch("dango.web.routes.ai.get_duckdb_path")
    @patch("dango.web.routes.ai.get_dbt_models")
    @patch("dango.web.routes.ai.load_sources_config")
    @patch("dango.web.routes.ai.get_project_root")
    @patch("dango.web.routes.ai.log_auth_event")
    def test_generated_at_is_iso_timestamp(
        self,
        mock_audit: MagicMock,
        mock_root: MagicMock,
        mock_sources: MagicMock,
        mock_dbt: MagicMock,
        mock_db_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        """generated_at is a valid ISO timestamp."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root
        mock_sources.return_value = []
        mock_dbt.return_value = []
        mock_db_path.return_value = tmp_path / "data" / "warehouse.duckdb"

        resp = client.get("/api/catalog/summary")
        assert resp.status_code == 200
        from datetime import datetime

        datetime.fromisoformat(resp.json()["generated_at"])
