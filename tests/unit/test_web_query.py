"""tests/unit/test_web_query.py

Tests for dango.web.routes.query — ad-hoc SQL query endpoint.
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
from dango.web.routes.query import router

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
    """Create a minimal FastAPI app with the query router."""
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


# ---------------------------------------------------------------------------
# A. Auth / RBAC tests
# ---------------------------------------------------------------------------


class TestAuthRBAC:
    """Test authentication and role-based access control."""

    def test_unauthenticated_returns_401(self, tmp_path: Path) -> None:
        client = _setup_unauthenticated_client(tmp_path)
        resp = client.post("/api/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 401

    def test_viewer_returns_403(self, tmp_path: Path) -> None:
        client, _ = _setup_client(tmp_path, role=Role.VIEWER)
        resp = client.post("/api/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 403

    @patch("dango.web.routes.query.get_duckdb_path")
    @patch("dango.web.routes.query._execute_query")
    @patch("dango.web.routes.query.log_auth_event")
    def test_editor_returns_200(
        self,
        mock_audit: MagicMock,
        mock_exec: MagicMock,
        mock_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)
        db_file.touch()
        mock_path.return_value = db_file
        mock_exec.return_value = {
            "columns": ["one"],
            "rows": [[1]],
            "row_count": 1,
            "truncated": False,
        }
        client, _ = _setup_client(tmp_path, role=Role.EDITOR)
        resp = client.post("/api/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 200

    @patch("dango.web.routes.query.get_duckdb_path")
    @patch("dango.web.routes.query._execute_query")
    @patch("dango.web.routes.query.log_auth_event")
    def test_admin_returns_200(
        self,
        mock_audit: MagicMock,
        mock_exec: MagicMock,
        mock_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)
        db_file.touch()
        mock_path.return_value = db_file
        mock_exec.return_value = {
            "columns": ["one"],
            "rows": [[1]],
            "row_count": 1,
            "truncated": False,
        }
        client, _ = _setup_client(tmp_path, role=Role.ADMIN)
        resp = client.post("/api/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# B. Input validation tests
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Test SQL input validation."""

    def test_empty_sql_returns_400(self, tmp_path: Path) -> None:
        client, _ = _setup_client(tmp_path)
        resp = client.post("/api/query", json={"sql": "   "})
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "DANGO-Q001"

    def test_sql_too_long_returns_400(self, tmp_path: Path) -> None:
        client, _ = _setup_client(tmp_path)
        long_sql = "SELECT " + "x" * 200_000
        resp = client.post("/api/query", json={"sql": long_sql})
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "DANGO-Q001"

    def test_insert_rejected(self, tmp_path: Path) -> None:
        client, _ = _setup_client(tmp_path)
        resp = client.post(
            "/api/query",
            json={"sql": "INSERT INTO t VALUES (1)"},
        )
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "DANGO-Q002"

    def test_update_rejected(self, tmp_path: Path) -> None:
        client, _ = _setup_client(tmp_path)
        resp = client.post(
            "/api/query",
            json={"sql": "UPDATE t SET x = 1"},
        )
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "DANGO-Q002"

    def test_delete_rejected(self, tmp_path: Path) -> None:
        client, _ = _setup_client(tmp_path)
        resp = client.post(
            "/api/query",
            json={"sql": "DELETE FROM t"},
        )
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "DANGO-Q002"

    def test_drop_rejected(self, tmp_path: Path) -> None:
        client, _ = _setup_client(tmp_path)
        resp = client.post(
            "/api/query",
            json={"sql": "DROP TABLE t"},
        )
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "DANGO-Q002"

    def test_multi_statement_rejected(self, tmp_path: Path) -> None:
        client, _ = _setup_client(tmp_path)
        resp = client.post(
            "/api/query",
            json={"sql": "SELECT 1; SELECT 2"},
        )
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "DANGO-Q002"


# ---------------------------------------------------------------------------
# C. Execution tests
# ---------------------------------------------------------------------------


class TestExecution:
    """Test query execution and error handling."""

    @patch("dango.web.routes.query.get_duckdb_path")
    @patch("dango.web.routes.query._execute_query")
    @patch("dango.web.routes.query.log_auth_event")
    def test_successful_select(
        self,
        mock_audit: MagicMock,
        mock_exec: MagicMock,
        mock_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)
        db_file.touch()
        mock_path.return_value = db_file
        mock_exec.return_value = {
            "columns": ["id", "name"],
            "rows": [[1, "alice"], [2, "bob"]],
            "row_count": 2,
            "truncated": False,
        }
        client, _ = _setup_client(tmp_path)
        resp = client.post("/api/query", json={"sql": "SELECT id, name FROM users"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["columns"] == ["id", "name"]
        assert data["rows"] == [[1, "alice"], [2, "bob"]]
        assert data["row_count"] == 2
        assert data["truncated"] is False
        assert data["warning"] is None

    @patch("dango.web.routes.query.get_duckdb_path")
    def test_warehouse_not_found(
        self,
        mock_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_path.return_value = tmp_path / "nonexistent" / "warehouse.duckdb"
        client, _ = _setup_client(tmp_path)
        resp = client.post("/api/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "DANGO-Q003"

    @patch("dango.web.routes.query.get_duckdb_path")
    @patch("dango.web.routes.query._execute_query")
    def test_timeout_returns_408(
        self,
        mock_exec: MagicMock,
        mock_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        import asyncio

        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)
        db_file.touch()
        mock_path.return_value = db_file
        mock_exec.side_effect = asyncio.TimeoutError()
        client, _ = _setup_client(tmp_path)
        resp = client.post("/api/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 408
        assert resp.json()["error_code"] == "DANGO-Q004"

    @patch("dango.web.routes.query.get_duckdb_path")
    @patch("dango.web.routes.query._execute_query")
    def test_duckdb_error_returns_400(
        self,
        mock_exec: MagicMock,
        mock_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        import duckdb

        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)
        db_file.touch()
        mock_path.return_value = db_file
        mock_exec.side_effect = duckdb.Error("some internal error")
        client, _ = _setup_client(tmp_path)
        resp = client.post("/api/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "DANGO-Q005"
        # Must NOT leak the raw error message
        assert "some internal error" not in resp.json()["message"]

    @patch("dango.web.routes.query.get_duckdb_path")
    @patch("dango.web.routes.query._execute_query")
    def test_unexpected_error_returns_500(
        self,
        mock_exec: MagicMock,
        mock_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)
        db_file.touch()
        mock_path.return_value = db_file
        mock_exec.side_effect = RuntimeError("something unexpected")
        client, _ = _setup_client(tmp_path)
        resp = client.post("/api/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 500
        assert resp.json()["error_code"] == "DANGO-Q006"
        assert "something unexpected" not in resp.json()["message"]

    @patch("dango.web.routes.query.get_duckdb_path")
    @patch("dango.web.routes.query._execute_query")
    @patch("dango.web.routes.query.log_auth_event")
    def test_truncation_includes_warning(
        self,
        mock_audit: MagicMock,
        mock_exec: MagicMock,
        mock_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)
        db_file.touch()
        mock_path.return_value = db_file
        mock_exec.return_value = {
            "columns": ["x"],
            "rows": [[i] for i in range(10_000)],
            "row_count": 10_000,
            "truncated": True,
        }
        client, _ = _setup_client(tmp_path)
        resp = client.post("/api/query", json={"sql": "SELECT x FROM big_table"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["truncated"] is True
        assert data["warning"] is not None
        assert "10000" in data["warning"]

    @patch("dango.web.routes.query.get_duckdb_path")
    @patch("dango.web.routes.query._execute_query")
    @patch("dango.web.routes.query.log_auth_event")
    def test_audit_event_logged(
        self,
        mock_audit: MagicMock,
        mock_exec: MagicMock,
        mock_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)
        db_file.touch()
        mock_path.return_value = db_file
        mock_exec.return_value = {
            "columns": ["one"],
            "rows": [[1]],
            "row_count": 1,
            "truncated": False,
        }
        client, _ = _setup_client(tmp_path)
        resp = client.post("/api/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[0][0] == AuditEvent.QUERY_EXECUTED
        assert call_kwargs[1]["user_id"] == "u-test-1"
        assert call_kwargs[1]["details"]["sql_length"] == 8
        assert call_kwargs[1]["details"]["row_count"] == 1


# ---------------------------------------------------------------------------
# D. SQL validation edge cases
# ---------------------------------------------------------------------------


class TestSQLValidation:
    """Test SQL validation edge cases with real sqlglot parsing."""

    def test_subquery_select_allowed(self, tmp_path: Path) -> None:
        """Subquery SELECTs should pass validation."""
        from dango.web.routes.query import _validate_sql

        # Should not raise
        _validate_sql("SELECT * FROM (SELECT 1 AS x)")

    def test_cte_select_allowed(self, tmp_path: Path) -> None:
        """CTE (WITH) SELECTs should pass validation."""
        from dango.web.routes.query import _validate_sql

        # Should not raise
        _validate_sql("WITH cte AS (SELECT 1 AS x) SELECT * FROM cte")

    def test_create_table_rejected(self, tmp_path: Path) -> None:
        """CREATE TABLE should be rejected."""
        from dango.web.routes.query import _validate_sql

        with pytest.raises(ValueError, match="SELECT"):
            _validate_sql("CREATE TABLE t (id INT)")

    def test_copy_to_rejected(self, tmp_path: Path) -> None:
        """COPY TO should be rejected (writes files, not blocked by read_only)."""
        from dango.web.routes.query import _validate_sql

        with pytest.raises(ValueError, match="SELECT"):
            _validate_sql("COPY (SELECT 1) TO '/tmp/out.csv'")

    def test_sqlglot_parse_failure_rejects_non_select(self) -> None:
        """When sqlglot fails to parse, keyword guard should reject non-SELECT."""
        from dango.web.routes.query import _validate_sql

        with patch("sqlglot.parse", side_effect=Exception("parse boom")):
            with pytest.raises(ValueError, match="SELECT"):
                _validate_sql("COPY (SELECT 1) TO '/tmp/out.csv'")

    def test_sqlglot_parse_failure_rejects_select_with_syntax_error(self) -> None:
        """When sqlglot fails to parse SELECT-like SQL, raise syntax error."""
        from dango.web.routes.query import _validate_sql

        with patch("sqlglot.parse", side_effect=Exception("parse boom")):
            with pytest.raises(ValueError, match="Invalid SQL syntax"):
                _validate_sql("SELECT some_duckdb_specific_syntax()")

    def test_fallback_rejects_non_select(self) -> None:
        """When sqlglot is unavailable, keyword guard should reject non-SELECT."""
        from dango.web.routes.query import _validate_sql

        with patch.dict("sys.modules", {"sqlglot": None, "sqlglot.expressions": None}):
            with pytest.raises(ValueError, match="SELECT"):
                _validate_sql("EXPORT DATABASE '/tmp/dump'")
