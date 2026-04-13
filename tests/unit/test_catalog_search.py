"""tests/unit/test_catalog_search.py

Unit tests for catalog search endpoint and column descriptions (BUG-016).
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


def _make_manifest(
    models: dict[str, dict[str, Any]] | None = None,
    sources: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal dbt manifest for testing."""
    nodes: dict[str, Any] = {}
    if models:
        for uid, m in models.items():
            nodes[uid] = {
                "resource_type": "model",
                "name": m.get("name", uid.split(".")[-1]),
                "schema": m.get("schema", "staging"),
                "config": {"materialized": m.get("materialized", "view")},
                "description": m.get("description", ""),
                "depends_on": {"nodes": m.get("depends_on", [])},
                "columns": m.get("columns", {}),
                "tags": m.get("tags", []),
            }

    src: dict[str, Any] = {}
    if sources:
        for uid, s in sources.items():
            src[uid] = {
                "name": s.get("name", uid.split(".")[-1]),
                "schema": s.get("schema", "raw_shop"),
                "description": s.get("description", ""),
                "columns": s.get("columns", {}),
                "resource_type": "source",
                "source_name": s.get("source_name", ""),
            }

    return {"nodes": nodes, "sources": src}


# ---------------------------------------------------------------------------
# GET /api/catalog/search
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSearchCatalog:
    """Tests for GET /api/catalog/search."""

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_search_by_name(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Search matches model names."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {"name": "stg_orders"},
                "model.proj.stg_customers": {"name": "stg_customers"},
            },
        )

        resp = client.get("/api/catalog/search?q=orders")

        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "orders"
        assert len(data["results"]) == 1
        assert data["results"][0]["name"] == "stg_orders"
        assert data["results"][0]["match_type"] == "name"

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_search_by_description(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Search matches model descriptions."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {
                    "name": "stg_orders",
                    "description": "Cleaned order data from chess.com",
                },
                "model.proj.stg_profiles": {
                    "name": "stg_profiles",
                    "description": "Player profiles",
                },
            },
        )

        resp = client.get("/api/catalog/search?q=chess")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["name"] == "stg_orders"
        assert data["results"][0]["match_type"] == "description"

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_search_by_column_name(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Search matches column names within models."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.fct_revenue": {
                    "name": "fct_revenue",
                    "schema": "marts",
                    "columns": {
                        "order_id": {"name": "order_id", "description": ""},
                        "total": {"name": "total", "description": ""},
                    },
                },
            },
        )

        resp = client.get("/api/catalog/search?q=order_id")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["name"] == "fct_revenue"
        assert data["results"][0]["match_type"] == "column"
        assert data["results"][0]["matched_column"] == "order_id"

    def test_short_query_returns_422(self, tmp_path: Path) -> None:
        """Query shorter than 2 characters returns 422 (FastAPI validation)."""
        client, _ = _setup_client(tmp_path)

        resp = client.get("/api/catalog/search?q=a")

        assert resp.status_code == 422

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_no_results(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Returns empty results list when nothing matches."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={"model.proj.stg_orders": {"name": "stg_orders"}},
        )

        resp = client.get("/api/catalog/search?q=zzzznotfound")

        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_requires_permission(self, tmp_path: Path) -> None:
        """Endpoint requires governance.view permission (viewer has it)."""
        client, _ = _setup_client(tmp_path, role=Role.VIEWER)
        resp = client.get("/api/catalog/search?q=orders")
        assert resp.status_code != 403

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_empty_results_when_no_manifest(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Returns empty results (not 404) when no manifest exists."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = None

        resp = client.get("/api/catalog/search?q=orders")

        assert resp.status_code == 200
        assert resp.json()["results"] == []


# ---------------------------------------------------------------------------
# Column descriptions in GET /api/catalog/{source}/{table}/columns
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestColumnsWithDescriptions:
    """Tests for column description field in existing columns endpoint."""

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_row_count")
    @patch("dango.web.routes.catalog._get_cached_stats")
    @patch("dango.web.routes.catalog._get_column_schema")
    @patch("dango.web.routes.catalog._table_exists")
    @patch("dango.web.routes.catalog._source_schema_exists")
    def test_descriptions_added_from_manifest(
        self,
        mock_schema_exists: MagicMock,
        mock_table_exists: MagicMock,
        mock_get_schema: MagicMock,
        mock_get_stats: MagicMock,
        mock_get_count: MagicMock,
        mock_get_profiled: MagicMock,
        mock_manifest: MagicMock,
        mock_get_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Column descriptions from manifest are merged into response."""
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
        mock_get_stats.return_value = {}
        mock_get_count.return_value = 100
        mock_get_profiled.return_value = None
        mock_manifest.return_value = {
            "nodes": {},
            "sources": {
                "source.proj.shop.orders": {
                    "name": "orders",
                    "schema": "raw_shopify",
                    "columns": {
                        "id": {"name": "id", "description": "Primary key"},
                        "email": {"name": "email", "description": "User email"},
                    },
                    "resource_type": "source",
                },
            },
        }

        resp = client.get("/api/catalog/shopify/orders/columns")

        assert resp.status_code == 200
        data = resp.json()
        cols = {c["name"]: c for c in data["columns"]}
        assert cols["id"]["description"] == "Primary key"
        assert cols["email"]["description"] == "User email"

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_row_count")
    @patch("dango.web.routes.catalog._get_cached_stats")
    @patch("dango.web.routes.catalog._get_column_schema")
    @patch("dango.web.routes.catalog._table_exists")
    @patch("dango.web.routes.catalog._source_schema_exists")
    def test_descriptions_null_without_manifest(
        self,
        mock_schema_exists: MagicMock,
        mock_table_exists: MagicMock,
        mock_get_schema: MagicMock,
        mock_get_stats: MagicMock,
        mock_get_count: MagicMock,
        mock_get_profiled: MagicMock,
        mock_manifest: MagicMock,
        mock_get_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Column descriptions are None when no manifest exists."""
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
        mock_manifest.return_value = None

        resp = client.get("/api/catalog/shopify/orders/columns")

        assert resp.status_code == 200
        data = resp.json()
        assert data["columns"][0]["description"] is None
