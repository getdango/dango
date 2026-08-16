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

from dango.auth.audit import AuditEvent
from dango.auth.models import Role, User
from dango.exceptions import (
    AuthenticationError,
    AuthorizationError,
    DangoError,
    ValidationError,
)
from dango.web.routes.catalog import _model_profiling_key, router

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

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_response_includes_all_model_types(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_get_root: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Response includes staging, intermediate, and marts models."""
        client, _ = _setup_client(tmp_path)
        mock_get_root.return_value = tmp_path
        mock_helpers_root.return_value = tmp_path
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

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_model_type_classification(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_get_root: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Models are classified by schema name first, then name prefix."""
        client, _ = _setup_client(tmp_path)
        mock_get_root.return_value = tmp_path
        mock_helpers_root.return_value = tmp_path
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

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_test_counts_from_run_results(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_get_root: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test counts and pass/fail from run_results are correct."""
        client, _ = _setup_client(tmp_path)
        mock_get_root.return_value = tmp_path
        mock_helpers_root.return_value = tmp_path
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

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_documentation_counts(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_get_root: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Columns total and documented counts are correct."""
        client, _ = _setup_client(tmp_path)
        mock_get_root.return_value = tmp_path
        mock_helpers_root.return_value = tmp_path
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

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_empty_when_no_manifest(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_get_root: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Returns empty lists (not 404) when no manifest exists."""
        client, _ = _setup_client(tmp_path)
        mock_get_root.return_value = tmp_path
        mock_helpers_root.return_value = tmp_path
        mock_manifest.return_value = None
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models")

        assert resp.status_code == 200
        data = resp.json()
        assert data["models"] == []
        assert data["sources"] == []

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_tags_included(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_get_root: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Tags array from manifest is included in response."""
        client, _ = _setup_client(tmp_path)
        mock_get_root.return_value = tmp_path
        mock_helpers_root.return_value = tmp_path
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

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_sources_included(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_get_root: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Sources from manifest appear in response with source_name."""
        client, _ = _setup_client(tmp_path)
        mock_get_root.return_value = tmp_path
        mock_helpers_root.return_value = tmp_path
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

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_404_model_not_found(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """404 when model name is not in manifest."""
        client, _ = _setup_client(tmp_path)
        mock_root.return_value = tmp_path
        mock_manifest.return_value = _make_manifest(
            models={"model.proj.stg_orders": {"name": "stg_orders"}},
        )
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models/nonexistent")

        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["detail"]

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_404_no_manifest(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """404 when no manifest exists."""
        client, _ = _setup_client(tmp_path)
        mock_root.return_value = tmp_path
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

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_cached_stats")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_model_column_schema")
    @patch("dango.web.routes.catalog._get_raw_tables_from_duckdb")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_raw_table_fallback_when_not_in_manifest(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_raw_tables: MagicMock,
        mock_col_schema: MagicMock,
        mock_profiled: MagicMock,
        mock_cached_stats: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Raw DuckDB table returned when not in manifest (BUG-132)."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_root.return_value = project_root
        mock_manifest.return_value = _make_manifest()  # empty manifest
        mock_run_results.return_value = None
        mock_raw_tables.return_value = [
            {"schema": "raw_shop", "table": "child_table", "source_name": "shop"},
        ]
        mock_col_schema.return_value = [
            {"name": "id", "type": "BIGINT", "nullable": False},
            {"name": "value", "type": "VARCHAR", "nullable": True},
        ]
        mock_profiled.return_value = "2026-04-30T10:00:00Z"
        mock_cached_stats.return_value = {
            "id": {"null_pct": "0", "distinct_count": "42"},
        }

        resp = client.get("/api/catalog/models/child_table")

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "child_table"
        assert data["type"] == "source"
        assert data["source_name"] == "shop"
        assert data["profiled_at"] == "2026-04-30T10:00:00Z"
        assert len(data["columns"]) == 2
        assert data["columns"][0]["stats"] == {"null_pct": "0", "distinct_count": "42"}
        assert data["columns"][1]["stats"] is None

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_raw_tables_from_duckdb")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_404_when_not_in_manifest_or_duckdb(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_raw_tables: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Still 404 when model not in manifest AND not in DuckDB raw schemas."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_root.return_value = project_root
        mock_manifest.return_value = _make_manifest()
        mock_run_results.return_value = None
        mock_raw_tables.return_value = []  # no raw tables either

        resp = client.get("/api/catalog/models/nonexistent")

        assert resp.status_code == 404

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_cached_stats")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_model_column_schema")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_source_detail_includes_cached_stats(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_col_schema: MagicMock,
        mock_profiled: MagicMock,
        mock_cached_stats: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Source detail response injects cached profiling stats (BUG-134)."""
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
                    "schema": "raw_shop",
                },
            },
        )
        mock_run_results.return_value = None
        mock_col_schema.return_value = [
            {"name": "id", "type": "BIGINT", "nullable": False},
            {"name": "amount", "type": "DOUBLE", "nullable": True},
        ]
        mock_profiled.return_value = "2026-04-30T12:00:00Z"
        mock_cached_stats.return_value = {
            "id": {"null_pct": "0", "distinct_count": "100", "min": "1", "max": "100"},
        }

        resp = client.get("/api/catalog/models/orders")

        assert resp.status_code == 200
        data = resp.json()
        cols = {c["name"]: c for c in data["columns"]}
        assert cols["id"]["stats"]["null_pct"] == "0"
        assert cols["id"]["stats"]["distinct_count"] == "100"
        assert cols["amount"]["stats"] is None


# ---------------------------------------------------------------------------
# Raw table discovery helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRawTableDiscovery:
    """Tests for _get_raw_tables_from_duckdb and list endpoint integration."""

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog._get_source_summary_stats")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_raw_tables_not_appended_to_sources(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_root: MagicMock,
        mock_source_stats: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Raw tables are NOT injected into catalog list (raw table discovery removed)."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()
        mock_root.return_value = project_root
        mock_helpers_root.return_value = project_root
        mock_source_stats.return_value = {}

        mock_manifest.return_value = _make_manifest(
            sources={
                "source.proj.shop.orders": {
                    "name": "orders",
                    "source_name": "shop",
                },
            },
        )
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models")
        data = resp.json()

        source_names = [s["name"] for s in data["sources"]]
        assert "orders" in source_names  # from manifest
        assert len(source_names) == 1  # no raw tables injected

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog._get_source_summary_stats")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_overview_included_in_response(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_root: MagicMock,
        mock_source_stats: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Response includes overview with source/table/model counts (BUG-128)."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()
        mock_root.return_value = project_root
        mock_helpers_root.return_value = project_root
        mock_source_stats.return_value = {}

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
            },
        )
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models")
        data = resp.json()

        assert "overview" in data
        overview = data["overview"]
        assert overview["model_count"] == 1
        assert overview["table_count"] == 1  # 1 source
        assert isinstance(overview["freshness"], list)

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_overview_present_when_no_manifest(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_root: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Overview is present even when manifest is None."""
        client, _ = _setup_client(tmp_path)
        mock_root.return_value = tmp_path
        mock_helpers_root.return_value = tmp_path
        mock_manifest.return_value = None
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models")
        data = resp.json()

        assert "overview" in data
        assert data["overview"]["model_count"] == 0
        assert data["overview"]["table_count"] == 0


# ---------------------------------------------------------------------------
# BUG-155: Per-source breakdown in overview
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSourcesDetail:
    """Tests for sources_detail in catalog overview (BUG-155)."""

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog._get_source_summary_stats")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_sources_detail_in_overview(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_root: MagicMock,
        mock_source_stats: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """sources_detail contains per-source table count and row count."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()
        mock_root.return_value = project_root
        mock_helpers_root.return_value = project_root

        # Create sources config
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "sources.yml").write_text(
            "sources:\n  - name: shop\n    type: postgres\n  - name: crm\n    type: hubspot\n"
        )

        mock_manifest.return_value = _make_manifest()
        mock_run_results.return_value = None
        mock_source_stats.return_value = {
            "shop": {"table_count": 5, "estimated_row_total": 12000},
            "crm": {"table_count": 3, "estimated_row_total": 800},
        }

        resp = client.get("/api/catalog/models")

        assert resp.status_code == 200
        data = resp.json()
        overview = data["overview"]
        assert "sources_detail" in overview
        assert len(overview["sources_detail"]) == 2

        detail_map = {sd["name"]: sd for sd in overview["sources_detail"]}
        assert detail_map["shop"]["table_count"] == 5
        assert detail_map["shop"]["estimated_row_total"] == 12000
        assert detail_map["crm"]["table_count"] == 3
        assert detail_map["crm"]["estimated_row_total"] == 800

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog._get_source_summary_stats")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_sources_detail_missing_source_in_duckdb(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_root: MagicMock,
        mock_source_stats: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Source in config but not in DuckDB gets zero counts."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()
        mock_root.return_value = project_root
        mock_helpers_root.return_value = project_root

        # Source in config but not in DuckDB stats
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "sources.yml").write_text(
            "sources:\n  - name: shop\n    type: postgres\n  - name: new_source\n    type: stripe\n"
        )

        mock_manifest.return_value = _make_manifest()
        mock_run_results.return_value = None
        mock_source_stats.return_value = {
            "shop": {"table_count": 3, "estimated_row_total": 500},
            # new_source is NOT in DuckDB yet
        }

        resp = client.get("/api/catalog/models")

        assert resp.status_code == 200
        data = resp.json()
        detail_map = {sd["name"]: sd for sd in data["overview"]["sources_detail"]}
        assert detail_map["new_source"]["table_count"] == 0
        assert detail_map["new_source"]["estimated_row_total"] == 0


# ---------------------------------------------------------------------------
# S3-QG: row counts in list + profile-all trigger
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRowCountsAndProfileAll:
    """Tests for row counts in the list and the profile-all trigger."""

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_cached_row_counts")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_model_list_includes_row_count(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_row_counts: MagicMock,
        mock_get_root: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """List response includes row_count sourced from the profiling cache."""
        client, _ = _setup_client(tmp_path)
        mock_get_root.return_value = tmp_path
        mock_helpers_root.return_value = tmp_path
        mock_row_counts.return_value = {("marts", "fct_revenue"): 100}
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {"name": "stg_orders", "schema": "staging"},
                "model.proj.int_clean": {"name": "int_clean", "schema": "intermediate"},
                "model.proj.fct_revenue": {"name": "fct_revenue", "schema": "marts"},
            },
        )
        mock_run_results.return_value = None

        resp = client.get("/api/catalog/models")

        assert resp.status_code == 200
        models = {m["name"]: m for m in resp.json()["models"]}
        assert models["fct_revenue"]["row_count"] == 100
        assert models["stg_orders"]["row_count"] is None
        assert models["int_clean"]["row_count"] is None
        for m in models.values():
            assert "row_count" in m

    @patch("dango.web.helpers.get_project_root")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_cached_row_counts")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_source_list_includes_row_count(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_row_counts: MagicMock,
        mock_get_root: MagicMock,
        mock_helpers_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Sources in the list also carry row_count from the profiling cache."""
        client, _ = _setup_client(tmp_path)
        mock_get_root.return_value = tmp_path
        mock_helpers_root.return_value = tmp_path
        mock_row_counts.return_value = {("shop", "orders"): 50}
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

        assert resp.status_code == 200
        sources = {s["name"]: s for s in resp.json()["sources"]}
        assert sources["orders"]["row_count"] == 50

    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._get_cached_stats")
    @patch("dango.web.routes.catalog._get_profiled_at")
    @patch("dango.web.routes.catalog._get_model_column_schema")
    @patch("dango.web.routes.catalog._get_run_results")
    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_marts_model_reads_cached_stats(
        self,
        mock_manifest: MagicMock,
        mock_run_results: MagicMock,
        mock_col_schema: MagicMock,
        mock_profiled: MagicMock,
        mock_cached_stats: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Marts models read profiling stats keyed by their dbt schema."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_root.return_value = project_root
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.fct_revenue": {
                    "name": "fct_revenue",
                    "schema": "marts",
                    "columns": {"id": {"name": "id", "description": ""}},
                },
            },
        )
        mock_run_results.return_value = None
        mock_col_schema.return_value = [
            {"name": "id", "type": "BIGINT", "nullable": False},
        ]
        mock_profiled.return_value = "2026-05-01T10:00:00Z"
        mock_cached_stats.return_value = {"id": {"null_pct": "0", "distinct_count": "42"}}

        resp = client.get("/api/catalog/models/fct_revenue")

        assert resp.status_code == 200
        data = resp.json()
        mock_profiled.assert_called_once_with(project_root, "marts", "fct_revenue")
        mock_cached_stats.assert_called_once_with(project_root, "marts", "fct_revenue")
        assert data["profiled_at"] == "2026-05-01T10:00:00Z"
        assert data["columns"][0]["stats"] == {"null_pct": "0", "distinct_count": "42"}

    @patch("dango.web.routes.catalog.log_auth_event")
    @patch("dango.web.routes.catalog.get_project_root")
    @patch("dango.web.routes.catalog._run_profiling")
    @patch("dango.web.routes.catalog._get_raw_tables_from_duckdb")
    def test_profile_all_endpoint(
        self,
        mock_raw_tables: MagicMock,
        mock_run_profiling: MagicMock,
        mock_root: MagicMock,
        mock_log_auth: MagicMock,
        tmp_path: Path,
    ) -> None:
        """profile-all discovers raw sources, re-profiles, and audits the trigger."""
        client, project_root = _setup_client(tmp_path)
        db_dir = tmp_path / "data"
        db_dir.mkdir()
        (db_dir / "warehouse.duckdb").touch()

        mock_root.return_value = project_root
        mock_raw_tables.return_value = [
            {"schema": "raw_shop", "table": "orders", "source_name": "shop"},
            {"schema": "raw_shop", "table": "customers", "source_name": "shop"},
            {"schema": "raw_crm", "table": "contacts", "source_name": "crm"},
        ]

        resp = client.post("/api/catalog/profile-all")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["sources"] == ["crm", "shop"]
        mock_run_profiling.assert_called_once_with(project_root, ["crm", "shop"])
        mock_log_auth.assert_called_once()
        assert mock_log_auth.call_args[0][0] == AuditEvent.CATALOG_PROFILE_TRIGGERED

    @patch("dango.web.routes.catalog.get_project_root")
    def test_profile_all_requires_warehouse(
        self,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """profile-all returns 404 when no warehouse database exists."""
        client, project_root = _setup_client(tmp_path)
        mock_root.return_value = project_root

        resp = client.post("/api/catalog/profile-all")

        assert resp.status_code == 404

    def test_profile_all_requires_dbt_run_permission(self, tmp_path: Path) -> None:
        """profile-all requires dbt.run — a viewer gets 403."""
        client, _ = _setup_client(tmp_path, role=Role.VIEWER)

        resp = client.post("/api/catalog/profile-all")

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# S3-QG: profiling cache-key symmetry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModelProfilingKey:
    """Tests for _model_profiling_key — mirrors the profiling write path."""

    @pytest.mark.parametrize(
        ("node", "kind", "expected"),
        [
            # marts / intermediate models are keyed by their dbt schema
            (
                {"schema": "marts", "name": "fct_revenue", "alias": "fct_revenue"},
                "model",
                ("marts", "fct_revenue"),
            ),
            (
                {"schema": "intermediate", "name": "int_clean"},
                "model",
                ("intermediate", "int_clean"),
            ),
            # staging models with stg_{source}__ prefix are keyed by raw source
            (
                {"schema": "staging", "name": "stg_google_ads__campaigns"},
                "model",
                ("google_ads", "stg_google_ads__campaigns"),
            ),
            # staging model without the __ convention is not profiled by the write path
            ({"schema": "staging", "name": "stg_orders"}, "model", None),
            # alias is preferred over name (matches _profile_dbt_models)
            ({"schema": "marts", "name": "foo", "alias": "fct_bar"}, "model", ("marts", "fct_bar")),
            # model without a schema has no key
            ({"schema": "", "name": "orphan"}, "model", None),
            # sources are keyed by source_name
            ({"name": "orders", "source_name": "shop"}, "source", ("shop", "orders")),
            # source without a source_name has no key
            ({"name": "orders", "source_name": ""}, "source", None),
        ],
    )
    def test_key(
        self,
        node: dict[str, Any],
        kind: str,
        expected: tuple[str, str] | None,
    ) -> None:
        """_model_profiling_key produces the (source, table) write-path key."""
        assert _model_profiling_key(node, kind) == expected
