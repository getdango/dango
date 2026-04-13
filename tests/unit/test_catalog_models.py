"""tests/unit/test_catalog_models.py

Unit tests for catalog model list and detail endpoints (BUG-016).
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
    tests: dict[str, dict[str, Any]] | None = None,
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
                "meta": m.get("meta", {}),
                "raw_code": m.get("raw_code", ""),
                "compiled_code": m.get("compiled_code", ""),
            }
    if tests:
        for uid, t in tests.items():
            nodes[uid] = {
                "resource_type": "test",
                "name": t.get("name", uid.split(".")[-1]),
                "depends_on": {"nodes": t.get("depends_on", [])},
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


def _make_run_results(
    results: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a minimal run_results.json for testing."""
    return {
        "metadata": {"generated_at": "2026-04-13T10:00:00Z"},
        "results": results or [],
    }


# ---------------------------------------------------------------------------
# GET /api/catalog/models
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListCatalogModels:
    """Tests for GET /api/catalog/models."""

    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_response_includes_all_model_types(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Response includes staging, intermediate, and marts models."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            sources={
                "source.proj.shop.orders": {
                    "name": "orders",
                    "source_name": "shop",
                },
            },
            models={
                "model.proj.stg_orders": {
                    "name": "stg_orders",
                    "schema": "staging",
                },
                "model.proj.int_clean": {
                    "name": "int_clean",
                    "schema": "intermediate",
                },
                "model.proj.fct_revenue": {
                    "name": "fct_revenue",
                    "schema": "marts",
                },
            },
        )
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["models"]) == 3
        assert len(data["sources"]) == 1
        types = {m["type"] for m in data["models"]}
        assert types == {"staging", "intermediate", "marts"}

    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_model_type_classification(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Models are classified by schema name first, then name prefix."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {
                    "name": "stg_orders",
                    "schema": "staging",
                },
                "model.proj.dim_customers": {
                    "name": "dim_customers",
                    "schema": "marts",
                },
                "model.proj.some_model": {
                    "name": "some_model",
                    "schema": "custom_schema",
                },
            },
        )
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models")
        data = resp.json()

        model_map = {m["name"]: m["type"] for m in data["models"]}
        assert model_map["stg_orders"] == "staging"
        assert model_map["dim_customers"] == "marts"
        assert model_map["some_model"] == "intermediate"

    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_test_counts_from_run_results(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test counts and pass/fail from run_results are correct."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {"name": "stg_orders", "schema": "staging"},
            },
            tests={
                "test.proj.not_null": {
                    "name": "not_null_stg_orders_id",
                    "depends_on": ["model.proj.stg_orders"],
                },
                "test.proj.unique": {
                    "name": "unique_stg_orders_id",
                    "depends_on": ["model.proj.stg_orders"],
                },
            },
        )
        mock_run_results.return_value = _make_run_results(
            [
                {"unique_id": "test.proj.not_null", "status": "pass"},
                {"unique_id": "test.proj.unique", "status": "fail"},
            ]
        )

        resp = client.get("/api/catalog/models")
        data = resp.json()

        model = data["models"][0]
        assert model["test_count"] == 2
        assert model["tests_passing"] == 1
        assert model["tests_failing"] == 1

    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_documentation_counts(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Columns total and documented counts are correct."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {
                    "name": "stg_orders",
                    "schema": "staging",
                    "columns": {
                        "id": {"name": "id", "description": "Primary key"},
                        "amount": {"name": "amount", "description": ""},
                        "status": {"name": "status", "description": "Order status"},
                    },
                },
            },
        )
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models")
        data = resp.json()

        model = data["models"][0]
        assert model["columns_total"] == 3
        assert model["columns_documented"] == 2

    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_empty_when_no_manifest(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Returns empty lists (not 404) when no manifest exists."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = None
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models")

        assert resp.status_code == 200
        data = resp.json()
        assert data["models"] == []
        assert data["sources"] == []

    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_tags_included(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Tags array from manifest is included in response."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {
                    "name": "stg_orders",
                    "schema": "staging",
                    "tags": ["daily", "chess"],
                },
            },
        )
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models")
        data = resp.json()

        assert data["models"][0]["tags"] == ["daily", "chess"]

    def test_requires_permission(self, tmp_path: Path) -> None:
        """Endpoint requires governance.view permission (viewer has it)."""
        client, _ = _setup_client(tmp_path, role=Role.VIEWER)
        resp = client.get("/api/catalog/models")
        assert resp.status_code != 403

    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_sources_included(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Sources from manifest appear in response with source_name."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            sources={
                "source.proj.shop.orders": {
                    "name": "orders",
                    "source_name": "shop",
                    "schema": "raw_shop",
                },
            },
        )
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models")
        data = resp.json()

        assert len(data["sources"]) == 1
        assert data["sources"][0]["name"] == "orders"
        assert data["sources"][0]["type"] == "source"
        assert data["sources"][0]["source_name"] == "shop"


# ---------------------------------------------------------------------------
# GET /api/catalog/models/{model_name}
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetCatalogModel:
    """Tests for GET /api/catalog/models/{model_name}."""

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_model_column_schema")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_model_detail_response_shape(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_col_schema: MagicMock,
        mock_profiled: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Response has all required fields."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_root.return_value = project_root
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {
                    "name": "stg_orders",
                    "schema": "staging",
                    "description": "Cleaned orders",
                    "tags": ["daily"],
                    "raw_code": "SELECT * FROM {{ source('shop', 'orders') }}",
                    "compiled_code": "SELECT * FROM raw_shop.orders",
                },
            },
        )
        mock_run_results.return_value = None
        mock_col_schema.return_value = [
            {"name": "id", "type": "BIGINT", "nullable": False},
        ]
        mock_profiled.return_value = None

        resp = client.get("/api/catalog/models/stg_orders")

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "stg_orders"
        assert data["type"] == "staging"
        assert data["description"] == "Cleaned orders"
        assert data["tags"] == ["daily"]
        assert "raw_code" in data
        assert "compiled_code" in data
        assert "columns" in data
        assert "depends_on" in data
        assert "depended_on_by" in data

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_model_column_schema")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_includes_sql_code(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_col_schema: MagicMock,
        mock_profiled: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Raw and compiled SQL code from manifest is included."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_root.return_value = project_root
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {
                    "name": "stg_orders",
                    "schema": "staging",
                    "raw_code": "SELECT id FROM {{ source('shop', 'orders') }}",
                    "compiled_code": "SELECT id FROM raw_shop.orders",
                },
            },
        )
        mock_run_results.return_value = None
        mock_col_schema.return_value = []
        mock_profiled.return_value = None

        resp = client.get("/api/catalog/models/stg_orders")
        data = resp.json()

        assert data["raw_code"] == "SELECT id FROM {{ source('shop', 'orders') }}"
        assert data["compiled_code"] == "SELECT id FROM raw_shop.orders"

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_model_column_schema")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_column_descriptions_merged(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_col_schema: MagicMock,
        mock_profiled: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """DuckDB columns get descriptions merged from manifest."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_root.return_value = project_root
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {
                    "name": "stg_orders",
                    "schema": "staging",
                    "columns": {
                        "id": {"name": "id", "description": "Primary key"},
                        "amount": {"name": "amount", "description": ""},
                    },
                },
            },
        )
        mock_run_results.return_value = None
        mock_col_schema.return_value = [
            {"name": "id", "type": "BIGINT", "nullable": False},
            {"name": "amount", "type": "DOUBLE", "nullable": True},
        ]
        mock_profiled.return_value = None

        resp = client.get("/api/catalog/models/stg_orders")
        data = resp.json()

        cols = {c["name"]: c for c in data["columns"]}
        assert cols["id"]["description"] == "Primary key"
        assert cols["amount"]["description"] is None  # empty string → None

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_model_column_schema")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_column_tests_mapped(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_col_schema: MagicMock,
        mock_profiled: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Tests are mapped to the correct columns by name pattern."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_root.return_value = project_root
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {
                    "name": "stg_orders",
                    "schema": "staging",
                    "columns": {"id": {"name": "id", "description": ""}},
                },
            },
            tests={
                "test.proj.not_null_stg_orders_id": {
                    "name": "not_null_stg_orders_id",
                    "depends_on": ["model.proj.stg_orders"],
                },
            },
        )
        mock_run_results.return_value = _make_run_results(
            [{"unique_id": "test.proj.not_null_stg_orders_id", "status": "pass"}]
        )
        mock_col_schema.return_value = [
            {"name": "id", "type": "BIGINT", "nullable": False},
        ]
        mock_profiled.return_value = None

        resp = client.get("/api/catalog/models/stg_orders")
        data = resp.json()

        id_col = data["columns"][0]
        assert id_col["tests"] is not None
        assert len(id_col["tests"]) == 1
        assert id_col["tests"][0]["name"] == "not_null_stg_orders_id"
        assert id_col["tests"][0]["status"] == "pass"

    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_404_model_not_found(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        tmp_path: Path,
    ) -> None:
        """404 when model name is not in manifest."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={"model.proj.stg_orders": {"name": "stg_orders"}},
        )
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models/nonexistent")

        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["detail"]

    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_404_no_manifest(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        tmp_path: Path,
    ) -> None:
        """404 when no manifest exists."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = None
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models/stg_orders")

        assert resp.status_code == 404
        assert "manifest" in resp.json()["detail"].lower()

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_model_column_schema")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_model_preferred_over_source(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_col_schema: MagicMock,
        mock_profiled: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When model and source share a name, model is preferred."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_root.return_value = project_root
        mock_manifest.return_value = _make_manifest(
            sources={
                "source.proj.shop.orders": {
                    "name": "orders",
                    "source_name": "shop",
                },
            },
            models={
                "model.proj.orders": {
                    "name": "orders",
                    "schema": "marts",
                },
            },
        )
        mock_run_results.return_value = None
        mock_col_schema.return_value = []
        mock_profiled.return_value = None

        resp = client.get("/api/catalog/models/orders")

        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "marts"  # model, not source

    def test_requires_permission(self, tmp_path: Path) -> None:
        """Endpoint requires governance.view permission (viewer has it)."""
        client, _ = _setup_client(tmp_path, role=Role.VIEWER)
        resp = client.get("/api/catalog/models/stg_orders")
        assert resp.status_code != 403
