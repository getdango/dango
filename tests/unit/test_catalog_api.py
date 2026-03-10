"""tests/unit/test_catalog_api.py

Unit tests for dango/web/routes/catalog.py — catalog API endpoints.
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
from dango.web.routes.catalog import router

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
    """Create a minimal FastAPI app with the catalog router."""
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


# ---------------------------------------------------------------------------
# GET /api/catalog/{source}/{table}/columns
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetTableColumns:
    """Tests for GET /api/catalog/{source}/{table}/columns."""

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_row_count")
    @patch("dango.web.routes.catalog._get_cached_stats")
    @patch("dango.web.routes.catalog._get_column_schema")
    @patch("dango.web.routes.catalog._table_exists")
    @patch("dango.web.routes.catalog._source_schema_exists")
    def test_response_shape(
        self,
        mock_schema_exists: MagicMock,
        mock_table_exists: MagicMock,
        mock_get_schema: MagicMock,
        mock_get_stats: MagicMock,
        mock_get_count: MagicMock,
        mock_get_profiled: MagicMock,
        mock_get_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Response has correct shape with source, table, columns, etc."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_get_root.return_value = project_root
        mock_schema_exists.return_value = True
        mock_table_exists.return_value = True
        mock_get_schema.return_value = [
            {"name": "id", "type": "BIGINT", "nullable": False},
            {"name": "email", "type": "VARCHAR", "nullable": True},
        ]
        mock_get_stats.return_value = {
            "id": {"null_count": "0", "distinct_count": "100"},
        }
        mock_get_count.return_value = 100
        mock_get_profiled.return_value = "2026-03-10T14:30:00+00:00"

        resp = client.get("/api/catalog/shopify/orders/columns")

        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "shopify"
        assert data["table"] == "orders"
        assert data["row_count"] == 100
        assert data["profiled_at"] == "2026-03-10T14:30:00+00:00"
        assert len(data["columns"]) == 2
        assert data["columns"][0]["name"] == "id"
        assert data["columns"][0]["stats"] == {"null_count": "0", "distinct_count": "100"}
        assert data["columns"][1]["stats"] is None  # no cached stats for email

    @patch("dango.web.routes.catalog.get_project_root")
    def test_404_duckdb_missing(
        self,
        mock_get_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """404 when DuckDB file does not exist."""
        client, project_root = _setup_client(tmp_path)
        mock_get_root.return_value = project_root
        # No data dir created

        resp = client.get("/api/catalog/shopify/orders/columns")

        assert resp.status_code == 404
        assert "warehouse" in resp.json()["detail"].lower()

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._source_schema_exists")
    def test_404_source_missing(
        self,
        mock_schema_exists: MagicMock,
        mock_get_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """404 when source schema does not exist."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_get_root.return_value = project_root
        mock_schema_exists.return_value = False

        resp = client.get("/api/catalog/nonexistent/orders/columns")

        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["detail"]

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._table_exists")
    @patch("dango.web.routes.catalog._source_schema_exists")
    def test_404_table_missing(
        self,
        mock_schema_exists: MagicMock,
        mock_table_exists: MagicMock,
        mock_get_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """404 when table does not exist in source."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_get_root.return_value = project_root
        mock_schema_exists.return_value = True
        mock_table_exists.return_value = False

        resp = client.get("/api/catalog/shopify/nonexistent/columns")

        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["detail"]

    def test_400_invalid_source_name(self, tmp_path: Path) -> None:
        """400 for invalid source name (special characters)."""
        client, _ = _setup_client(tmp_path)

        resp = client.get("/api/catalog/invalid-source!/orders/columns")

        assert resp.status_code == 400

    def test_400_invalid_table_name(self, tmp_path: Path) -> None:
        """400 for invalid table name (special characters)."""
        client, _ = _setup_client(tmp_path)

        resp = client.get("/api/catalog/shopify/bad-table!/columns")

        assert resp.status_code == 400

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_row_count")
    @patch("dango.web.routes.catalog._get_cached_stats")
    @patch("dango.web.routes.catalog._get_column_schema")
    @patch("dango.web.routes.catalog._table_exists")
    @patch("dango.web.routes.catalog._source_schema_exists")
    def test_stats_null_when_no_cached_data(
        self,
        mock_schema_exists: MagicMock,
        mock_table_exists: MagicMock,
        mock_get_schema: MagicMock,
        mock_get_stats: MagicMock,
        mock_get_count: MagicMock,
        mock_get_profiled: MagicMock,
        mock_get_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Stats are null when no profiling data is cached."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_get_root.return_value = project_root
        mock_schema_exists.return_value = True
        mock_table_exists.return_value = True
        mock_get_schema.return_value = [
            {"name": "id", "type": "BIGINT", "nullable": False},
        ]
        mock_get_stats.return_value = {}
        mock_get_count.return_value = 50
        mock_get_profiled.return_value = None

        resp = client.get("/api/catalog/shopify/orders/columns")

        assert resp.status_code == 200
        data = resp.json()
        assert data["profiled_at"] is None
        assert data["columns"][0]["stats"] is None

    def test_requires_permission(self, tmp_path: Path) -> None:
        """Endpoint requires governance.view permission (viewer role has it)."""
        client, project_root = _setup_client(tmp_path, role=Role.VIEWER)
        # Viewer has governance.view, so this should not be a 403
        # The request will fail for other reasons (no duckdb) but not auth
        resp = client.get("/api/catalog/shopify/orders/columns")
        assert resp.status_code != 403


# ---------------------------------------------------------------------------
# POST /api/catalog/{source}/{table}/profile
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRefreshTableProfile:
    """Tests for POST /api/catalog/{source}/{table}/profile."""

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_row_count")
    @patch("dango.web.routes.catalog._get_column_schema")
    @patch("dango.web.routes.catalog.profile_table")
    @patch("dango.web.routes.catalog._table_exists")
    @patch("dango.web.routes.catalog._source_schema_exists")
    def test_returns_fresh_stats(
        self,
        mock_schema_exists: MagicMock,
        mock_table_exists: MagicMock,
        mock_profile: MagicMock,
        mock_get_schema: MagicMock,
        mock_get_count: MagicMock,
        mock_get_profiled: MagicMock,
        mock_get_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Profile endpoint calls profile_table and returns fresh stats."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_get_root.return_value = project_root
        mock_schema_exists.return_value = True
        mock_table_exists.return_value = True
        mock_profile.return_value = {
            "id": {"null_count": "0", "distinct_count": "50", "min": "1", "max": "50"},
        }
        mock_get_schema.return_value = [
            {"name": "id", "type": "BIGINT", "nullable": False},
        ]
        mock_get_count.return_value = 50
        mock_get_profiled.return_value = "2026-03-10T15:00:00+00:00"

        resp = client.post("/api/catalog/shopify/orders/profile")

        assert resp.status_code == 200
        data = resp.json()
        assert data["columns"][0]["stats"]["null_count"] == "0"
        mock_profile.assert_called_once()

    @patch("dango.web.routes.catalog.get_project_root")
    def test_404_duckdb_missing(
        self,
        mock_get_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """404 when DuckDB file does not exist."""
        client, project_root = _setup_client(tmp_path)
        mock_get_root.return_value = project_root

        resp = client.post("/api/catalog/shopify/orders/profile")

        assert resp.status_code == 404

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._table_exists")
    @patch("dango.web.routes.catalog._source_schema_exists")
    def test_404_table_missing(
        self,
        mock_schema_exists: MagicMock,
        mock_table_exists: MagicMock,
        mock_get_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """404 when table does not exist."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_get_root.return_value = project_root
        mock_schema_exists.return_value = True
        mock_table_exists.return_value = False

        resp = client.post("/api/catalog/shopify/nonexistent/profile")

        assert resp.status_code == 404

    def test_400_invalid_source(self, tmp_path: Path) -> None:
        """400 for invalid source name."""
        client, _ = _setup_client(tmp_path)

        resp = client.post("/api/catalog/bad-source!/orders/profile")

        assert resp.status_code == 400

    def test_requires_permission(self, tmp_path: Path) -> None:
        """Endpoint requires governance.view permission."""
        client, _ = _setup_client(tmp_path, role=Role.VIEWER)
        resp = client.post("/api/catalog/shopify/orders/profile")
        assert resp.status_code != 403
