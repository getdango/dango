"""tests/unit/test_web_governance_overrides.py

Tests for PII override web endpoints in dango/web/routes/governance.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.exceptions import DangoError
from dango.web.routes.governance import router

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}


def _make_user(role: Role = Role.ADMIN) -> User:
    return User(email=f"{role.value}@test.com", role=role, password_hash="x")


def _make_app(project_root: Path, user: User) -> FastAPI:
    from dango.exceptions import AuthorizationError

    app = FastAPI()
    app.state.project_root = project_root

    status_map: dict[type[DangoError], int] = {
        AuthorizationError: 403,
        DangoError: 500,
    }

    @app.exception_handler(DangoError)
    async def dango_error_handler(request: Request, exc: DangoError) -> JSONResponse:
        status_code = 500
        for cls in type(exc).__mro__:
            if cls in status_map:
                status_code = status_map[cls]
                break
        return JSONResponse(
            status_code=status_code,
            content={"error_code": exc.error_code, "message": exc.user_message},
        )

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    app.include_router(router)
    return app


@pytest.fixture(autouse=True)
def _patch_project_root(tmp_path: Path) -> Any:
    """Patch get_project_root so the governance router uses the test tmp_path."""
    with patch("dango.web.routes.governance.get_project_root", return_value=tmp_path):
        yield


def _make_client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _init_db(tmp_path: Path) -> None:
    from dango.utils.dango_db import connect

    with connect(tmp_path):
        pass


# ---------------------------------------------------------------------------
# Tests: PUT /api/governance/pii/override
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSetPiiOverrideEndpoint:
    """Tests for PUT /api/governance/pii/override."""

    def test_set_override_success(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        admin = _make_user(Role.ADMIN)
        client = _make_client(_make_app(tmp_path, admin))
        resp = client.put(
            "/api/governance/pii/override",
            json={
                "source": "chess",
                "table_name": "games",
                "column_name": "pgn",
                "pii_status": "not_pii",
                "reason": "chess notation",
            },
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_set_override_invalid_status(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        admin = _make_user(Role.ADMIN)
        client = _make_client(_make_app(tmp_path, admin))
        resp = client.put(
            "/api/governance/pii/override",
            json={
                "source": "chess",
                "table_name": "games",
                "column_name": "pgn",
                "pii_status": "maybe",
            },
            headers=_HEADERS,
        )
        assert resp.status_code == 422

    def test_set_override_viewer_forbidden(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        viewer = _make_user(Role.VIEWER)
        client = _make_client(_make_app(tmp_path, viewer))
        resp = client.put(
            "/api/governance/pii/override",
            json={
                "source": "chess",
                "table_name": "games",
                "column_name": "pgn",
                "pii_status": "not_pii",
            },
            headers=_HEADERS,
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Tests: DELETE /api/governance/pii/override
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeletePiiOverrideEndpoint:
    """Tests for DELETE /api/governance/pii/override."""

    def test_delete_existing(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        from dango.governance.pii_overrides import set_pii_override

        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "test@test.com")
        admin = _make_user(Role.ADMIN)
        client = _make_client(_make_app(tmp_path, admin))
        resp = client.delete(
            "/api/governance/pii/override?source=chess&table=games&column=pgn",
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_delete_nonexistent(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        admin = _make_user(Role.ADMIN)
        client = _make_client(_make_app(tmp_path, admin))
        resp = client.delete(
            "/api/governance/pii/override?source=chess&table=games&column=pgn",
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_found"

    def test_delete_viewer_forbidden(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        viewer = _make_user(Role.VIEWER)
        client = _make_client(_make_app(tmp_path, viewer))
        resp = client.delete(
            "/api/governance/pii/override?source=chess&table=games&column=pgn",
            headers=_HEADERS,
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Tests: GET /api/governance/pii/overrides
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListPiiOverridesEndpoint:
    """Tests for GET /api/governance/pii/overrides."""

    def test_list_empty(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        admin = _make_user(Role.ADMIN)
        client = _make_client(_make_app(tmp_path, admin))
        resp = client.get("/api/governance/pii/overrides")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overrides"] == []
        assert data["count"] == 0

    def test_list_with_data(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        from dango.governance.pii_overrides import set_pii_override

        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "test@test.com")
        admin = _make_user(Role.ADMIN)
        client = _make_client(_make_app(tmp_path, admin))
        resp = client.get("/api/governance/pii/overrides")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["overrides"][0]["column_name"] == "pgn"

    def test_list_viewer_allowed(self, tmp_path: Path) -> None:
        """governance.view is available to viewers."""
        _init_db(tmp_path)
        viewer = _make_user(Role.VIEWER)
        client = _make_client(_make_app(tmp_path, viewer))
        resp = client.get("/api/governance/pii/overrides")
        assert resp.status_code == 200
