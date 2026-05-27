"""tests/unit/test_catalog_lineage.py

Unit tests for catalog lineage and impact analysis endpoints.
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
    """Build a minimal dbt manifest for testing.

    Args:
        models: Model node overrides keyed by unique_id.
        sources: Source node overrides keyed by unique_id.
        tests: Test node overrides keyed by unique_id.

    Returns:
        Manifest dict with ``nodes`` and ``sources``.
    """
    nodes: dict[str, Any] = {}
    if models:
        for uid, m in models.items():
            nodes[uid] = {
                "resource_type": "model",
                "name": m.get("name", uid.split(".")[-1]),
                "schema": m.get("schema", "analytics"),
                "config": {"materialized": m.get("materialized", "view")},
                "description": m.get("description", ""),
                "depends_on": {"nodes": m.get("depends_on", [])},
                "columns": m.get("columns", {}),
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
            parts = uid.split(".")
            src[uid] = {
                "name": s.get("name", parts[-1] if parts else ""),
                "source_name": s.get("source_name", parts[2] if len(parts) > 2 else ""),
                "schema": s.get("schema", "raw_shop"),
                "description": s.get("description", ""),
                "columns": s.get("columns", {}),
                "resource_type": "source",
            }

    return {"nodes": nodes, "sources": src}


# ---------------------------------------------------------------------------
# GET /api/catalog/lineage
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetLineage:
    """Tests for GET /api/catalog/lineage."""

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_full_dag_response_shape(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Response has correct nodes/edges structure."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            sources={
                "source.proj.shop.orders": {"name": "orders"},
                "source.proj.shop.customers": {"name": "customers"},
            },
            models={
                "model.proj.stg_orders": {
                    "name": "stg_orders",
                    "depends_on": ["source.proj.shop.orders"],
                },
                "model.proj.stg_customers": {
                    "name": "stg_customers",
                    "depends_on": ["source.proj.shop.customers"],
                },
                "model.proj.fct_orders": {
                    "name": "fct_orders",
                    "depends_on": [
                        "model.proj.stg_orders",
                        "model.proj.stg_customers",
                    ],
                },
            },
        )

        resp = client.get("/api/catalog/lineage")

        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert len(data["nodes"]) == 5  # 2 sources + 3 models
        assert len(data["edges"]) == 4  # 4 dependency edges

        node_names = {n["name"] for n in data["nodes"]}
        assert node_names == {
            "orders",
            "customers",
            "stg_orders",
            "stg_customers",
            "fct_orders",
        }

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_reverse_lookup_correct(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """depended_on_by is populated correctly."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            sources={
                "source.proj.shop.orders": {"name": "orders"},
            },
            models={
                "model.proj.stg_orders": {
                    "name": "stg_orders",
                    "depends_on": ["source.proj.shop.orders"],
                },
                "model.proj.fct_orders": {
                    "name": "fct_orders",
                    "depends_on": ["model.proj.stg_orders"],
                },
            },
        )

        resp = client.get("/api/catalog/lineage")
        data = resp.json()

        # Source 'orders' should be depended on by stg_orders
        source_node = next(n for n in data["nodes"] if n["name"] == "orders")
        assert "model.proj.stg_orders" in source_node["depended_on_by"]

        # stg_orders should be depended on by fct_orders
        stg_node = next(n for n in data["nodes"] if n["name"] == "stg_orders")
        assert "model.proj.fct_orders" in stg_node["depended_on_by"]

        # fct_orders is a leaf — no downstream
        fct_node = next(n for n in data["nodes"] if n["name"] == "fct_orders")
        assert fct_node["depended_on_by"] == []

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_test_associations(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test nodes are mapped back to their parent models."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {"name": "stg_orders"},
            },
            tests={
                "test.proj.not_null_stg_orders_id": {
                    "name": "not_null_stg_orders_id",
                    "depends_on": ["model.proj.stg_orders"],
                },
                "test.proj.unique_stg_orders_id": {
                    "name": "unique_stg_orders_id",
                    "depends_on": ["model.proj.stg_orders"],
                },
            },
        )

        resp = client.get("/api/catalog/lineage")
        data = resp.json()

        stg_node = next(n for n in data["nodes"] if n["name"] == "stg_orders")
        assert stg_node["test_count"] == 2
        assert set(stg_node["test_names"]) == {
            "not_null_stg_orders_id",
            "unique_stg_orders_id",
        }

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_documentation_status(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Models report documentation status for model and columns."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.documented": {
                    "name": "documented",
                    "description": "A well-documented model",
                    "columns": {
                        "id": {"name": "id", "description": "Primary key"},
                        "name": {"name": "name", "description": ""},
                        "email": {"name": "email", "description": "User email"},
                    },
                },
                "model.proj.undocumented": {
                    "name": "undocumented",
                    "description": "",
                    "columns": {},
                },
            },
        )

        resp = client.get("/api/catalog/lineage")
        data = resp.json()

        doc_node = next(n for n in data["nodes"] if n["name"] == "documented")
        assert doc_node["has_description"] is True
        assert doc_node["columns_documented"] == 2  # id + email
        assert doc_node["columns_total"] == 3

        undoc_node = next(n for n in data["nodes"] if n["name"] == "undocumented")
        assert undoc_node["has_description"] is False
        assert undoc_node["columns_documented"] == 0
        assert undoc_node["columns_total"] == 0

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_404_no_manifest(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """404 when no dbt manifest exists."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = None

        resp = client.get("/api/catalog/lineage")

        assert resp.status_code == 404
        assert "manifest" in resp.json()["detail"].lower()

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_sources_included(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Source nodes appear in DAG with type 'source'."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            sources={
                "source.proj.shop.orders": {"name": "orders"},
            },
            models={
                "model.proj.stg_orders": {
                    "name": "stg_orders",
                    "depends_on": ["source.proj.shop.orders"],
                },
            },
        )

        resp = client.get("/api/catalog/lineage")
        data = resp.json()

        source_nodes = [n for n in data["nodes"] if n["type"] == "source"]
        assert len(source_nodes) == 1
        assert source_nodes[0]["name"] == "orders"
        assert source_nodes[0]["materialization"] is None

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_source_nodes_include_source_name(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Source nodes in lineage include source_name for disambiguation."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            sources={
                "source.proj.shopify.orders": {
                    "name": "orders",
                    "source_name": "shopify",
                },
                "source.proj.stripe.orders": {
                    "name": "orders",
                    "source_name": "stripe",
                },
            },
        )

        resp = client.get("/api/catalog/lineage")
        data = resp.json()

        source_nodes = [n for n in data["nodes"] if n["type"] == "source"]
        assert len(source_nodes) == 2
        source_names = {n["source_name"] for n in source_nodes}
        assert source_names == {"shopify", "stripe"}
        assert all(n["name"] == "orders" for n in source_nodes)

    def test_requires_permission(self, tmp_path: Path) -> None:
        """Endpoint requires governance.view permission (viewer has it)."""
        client, _ = _setup_client(tmp_path, role=Role.VIEWER)
        resp = client.get("/api/catalog/lineage")
        assert resp.status_code != 403


# ---------------------------------------------------------------------------
# GET /api/catalog/impact/{model_name}
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetImpact:
    """Tests for GET /api/catalog/impact/{model_name}."""

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_recursive_tree(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Chain A→B→C returns tree with B and C as descendants."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            sources={
                "source.proj.shop.raw": {"name": "raw"},
            },
            models={
                "model.proj.stg": {
                    "name": "stg",
                    "depends_on": ["source.proj.shop.raw"],
                },
                "model.proj.fct": {
                    "name": "fct",
                    "depends_on": ["model.proj.stg"],
                },
            },
        )

        resp = client.get("/api/catalog/impact/raw")

        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "raw"
        assert "stg" in data["direct_dependents"]
        assert data["total_downstream_count"] == 2  # stg + fct

        # Verify tree structure
        tree = data["tree"]
        assert tree["name"] == "raw"
        assert len(tree["children"]) == 1
        assert tree["children"][0]["name"] == "stg"
        assert len(tree["children"][0]["children"]) == 1
        assert tree["children"][0]["children"][0]["name"] == "fct"

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_model_not_found(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """404 for nonexistent model."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_orders": {"name": "stg_orders"},
            },
        )

        resp = client.get("/api/catalog/impact/nonexistent")

        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["detail"]

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_model_no_dependents(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Leaf model returns empty tree."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.leaf": {"name": "leaf"},
            },
        )

        resp = client.get("/api/catalog/impact/leaf")

        assert resp.status_code == 200
        data = resp.json()
        assert data["direct_dependents"] == []
        assert data["total_downstream_count"] == 0
        assert data["tree"]["children"] == []

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_circular_dependency_detection(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Circular dependencies are flagged with cycle=True."""
        client, _ = _setup_client(tmp_path)
        # Manually build a manifest with a cycle: A → B → A
        manifest: dict[str, Any] = {
            "nodes": {
                "model.proj.a": {
                    "resource_type": "model",
                    "name": "a",
                    "schema": "analytics",
                    "config": {"materialized": "view"},
                    "description": "",
                    "depends_on": {"nodes": ["model.proj.b"]},
                    "columns": {},
                },
                "model.proj.b": {
                    "resource_type": "model",
                    "name": "b",
                    "schema": "analytics",
                    "config": {"materialized": "view"},
                    "description": "",
                    "depends_on": {"nodes": ["model.proj.a"]},
                    "columns": {},
                },
            },
            "sources": {},
        }
        mock_manifest.return_value = manifest

        resp = client.get("/api/catalog/impact/a")

        assert resp.status_code == 200
        tree = resp.json()["tree"]
        # a → b → a(cycle)
        assert tree["name"] == "a"
        assert len(tree["children"]) == 1
        b_node = tree["children"][0]
        assert b_node["name"] == "b"
        assert len(b_node["children"]) == 1
        cycle_node = b_node["children"][0]
        assert cycle_node["cycle"] is True
        assert cycle_node["children"] == []

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_diamond_dependency_not_flagged_as_cycle(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Diamond deps expand fully under each branch (no false cycles)."""
        client, _ = _setup_client(tmp_path)
        # stg_base → model_a → final, stg_base → model_b → final
        mock_manifest.return_value = _make_manifest(
            models={
                "model.proj.stg_base": {
                    "name": "stg_base",
                },
                "model.proj.model_a": {
                    "name": "model_a",
                    "depends_on": ["model.proj.stg_base"],
                },
                "model.proj.model_b": {
                    "name": "model_b",
                    "depends_on": ["model.proj.stg_base"],
                },
                "model.proj.final": {
                    "name": "final",
                    "depends_on": [
                        "model.proj.model_a",
                        "model.proj.model_b",
                    ],
                },
            },
        )

        resp = client.get("/api/catalog/impact/stg_base")

        assert resp.status_code == 200
        tree = resp.json()["tree"]
        # stg_base has 2 direct children: model_a and model_b
        assert len(tree["children"]) == 2
        child_names = {c["name"] for c in tree["children"]}
        assert child_names == {"model_a", "model_b"}
        # Both branches should show "final" as a child — no cycle flag
        for child in tree["children"]:
            assert len(child["children"]) == 1
            grandchild = child["children"][0]
            assert grandchild["name"] == "final"
            assert "cycle" not in grandchild

    def test_400_invalid_model_name(self, tmp_path: Path) -> None:
        """400 for model name with special characters."""
        client, _ = _setup_client(tmp_path)

        resp = client.get("/api/catalog/impact/bad-name!")

        assert resp.status_code == 400

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_404_no_manifest(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """404 when no dbt manifest exists."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = None

        resp = client.get("/api/catalog/impact/orders")

        assert resp.status_code == 404

    @patch("dango.web.routes.catalog.get_dbt_manifest")
    def test_model_preferred_over_source_on_name_collision(
        self,
        mock_manifest: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When a source and model share a name, the model is preferred."""
        client, _ = _setup_client(tmp_path)
        mock_manifest.return_value = _make_manifest(
            sources={
                "source.proj.shop.orders": {"name": "orders"},
            },
            models={
                "model.proj.orders": {
                    "name": "orders",
                    "depends_on": ["source.proj.shop.orders"],
                },
            },
        )

        resp = client.get("/api/catalog/impact/orders")

        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "model"

    def test_requires_permission(self, tmp_path: Path) -> None:
        """Endpoint requires governance.view permission (viewer has it)."""
        client, _ = _setup_client(tmp_path, role=Role.VIEWER)
        resp = client.get("/api/catalog/impact/orders")
        assert resp.status_code != 403
