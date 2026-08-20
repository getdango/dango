"""tests/unit/test_variables_route.py

Tests for dango.web.routes.variables — environment variables and OAuth token management.
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
from dango.exceptions import DangoError
from dango.web.routes.variables import router

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _make_admin() -> User:
    """Create an admin user for tests."""
    return User(
        email="admin@test.com",
        password_hash="hashed",
        role=Role.ADMIN,
        is_active=True,
    )


def _make_editor() -> User:
    """Create an editor user for tests."""
    return User(
        email="editor@test.com",
        password_hash="hashed",
        role=Role.EDITOR,
        is_active=True,
    )


def _make_app(project_root: Path) -> FastAPI:
    """Create a minimal FastAPI app with the variables router."""
    from dango.exceptions import AuthenticationError, AuthorizationError

    app = FastAPI()
    app.state.project_root = project_root

    status_map: dict[type[DangoError], int] = {
        AuthenticationError: 401,
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

    app.include_router(router)
    return app


def _setup_client(tmp_path: Path, user: User) -> TestClient:
    """Create a test client with a user injected via middleware."""
    app = _make_app(tmp_path)

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


HEADERS: dict[str, str] = {
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/json",
}


@pytest.fixture()
def admin_client(tmp_path):
    """Admin client fixture."""
    return _setup_client(tmp_path, _make_admin()), tmp_path


@pytest.fixture()
def editor_client(tmp_path):
    """Editor client fixture (used for permission tests)."""
    return _setup_client(tmp_path, _make_editor()), tmp_path


# ---------------------------------------------------------------------------
# GET /api/variables — list env vars (masked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListVariables:
    """Tests for GET /api/variables."""

    def test_empty_list(self, admin_client):
        client, tmp_path = admin_client
        resp = client.get("/api/variables", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["variables"] == []

    def test_list_with_vars(self, admin_client):
        client, tmp_path = admin_client
        (tmp_path / ".env").write_text("SHORT=12\nLONG_KEY=this_is_a_long_value\n")
        resp = client.get("/api/variables", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["variables"]) == 2

        # Verify masking: long value shows last 4 chars, short value fully masked
        vars_by_key = {v["key"]: v for v in data["variables"]}
        assert vars_by_key["SHORT"]["masked_value"] == "****"
        assert vars_by_key["LONG_KEY"]["masked_value"] == "****alue"
        # Ensure actual values never appear
        assert "12" not in vars_by_key["SHORT"]["masked_value"]
        assert "this_is_" not in vars_by_key["LONG_KEY"]["masked_value"]


# ---------------------------------------------------------------------------
# POST /api/variables — add/update env var
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSetVariable:
    """Tests for POST /api/variables."""

    def test_create_new_variable(self, admin_client):
        client, tmp_path = admin_client
        resp = client.post(
            "/api/variables", json={"key": "NEW_KEY", "value": "new_value"}, headers=HEADERS
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "created" in data["message"]
        content = (tmp_path / ".env").read_text()
        assert "NEW_KEY=new_value" in content

    def test_update_existing_variable(self, admin_client):
        client, tmp_path = admin_client
        (tmp_path / ".env").write_text("KEY=old\n")
        resp = client.post("/api/variables", json={"key": "KEY", "value": "new"}, headers=HEADERS)
        assert resp.status_code == 200
        assert "updated" in resp.json()["message"]
        content = (tmp_path / ".env").read_text()
        assert "KEY=new" in content

    def test_empty_key_rejected(self, admin_client):
        client, _ = admin_client
        resp = client.post("/api/variables", json={"key": "", "value": "v"}, headers=HEADERS)
        assert resp.status_code == 400

    def test_missing_key_rejected(self, admin_client):
        client, _ = admin_client
        resp = client.post("/api/variables", json={"value": "v"}, headers=HEADERS)
        assert resp.status_code == 400

    def test_non_string_value_rejected(self, admin_client):
        client, _ = admin_client
        resp = client.post("/api/variables", json={"key": "K", "value": 123}, headers=HEADERS)
        assert resp.status_code == 400

    @patch("dango.web.routes.variables.log_auth_event")
    def test_audit_event_logged_with_source_page(self, mock_log, admin_client):
        client, _ = admin_client
        client.post("/api/variables", json={"key": "K", "value": "V"}, headers=HEADERS)
        mock_log.assert_called_once()
        from dango.auth.audit import AuditEvent

        assert mock_log.call_args[0][0] == AuditEvent.SECRET_SET
        # Verify source_page is in details
        details = mock_log.call_args[1]["details"]
        assert details["source_page"] == "variables"


# ---------------------------------------------------------------------------
# DELETE /api/variables/{key} — remove env var
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeleteVariable:
    """Tests for DELETE /api/variables/{key}."""

    def test_delete_existing_key(self, admin_client):
        client, tmp_path = admin_client
        (tmp_path / ".env").write_text("FOO=bar\nBAZ=qux\n")
        resp = client.delete("/api/variables/FOO", headers=HEADERS)
        assert resp.status_code == 200
        content = (tmp_path / ".env").read_text()
        assert "FOO" not in content
        assert "BAZ=qux" in content

    def test_delete_missing_key_returns_404(self, admin_client):
        client, _ = admin_client
        resp = client.delete("/api/variables/MISSING", headers=HEADERS)
        assert resp.status_code == 404

    @patch("dango.web.routes.variables.log_auth_event")
    def test_audit_event_logged_with_source_page(self, mock_log, admin_client):
        client, tmp_path = admin_client
        (tmp_path / ".env").write_text("K=V\n")
        client.delete("/api/variables/K", headers=HEADERS)
        mock_log.assert_called_once()
        from dango.auth.audit import AuditEvent

        assert mock_log.call_args[0][0] == AuditEvent.SECRET_DELETED
        # Verify source_page is in details
        details = mock_log.call_args[1]["details"]
        assert details["source_page"] == "variables"


# ---------------------------------------------------------------------------
# GET /api/variables/oauth — OAuth token status (read-only)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOAuthStatusDerivation:
    """Tests for _derive_oauth_status status derivation logic."""

    def test_status_ok(self):
        from dango.web.routes.variables import _derive_oauth_status

        mock_cred = MagicMock()
        mock_cred.is_expired.return_value = False
        mock_cred.is_expiring_soon.return_value = False

        mock_result = MagicMock()
        mock_result.valid = True
        mock_result.error_code = None

        status = _derive_oauth_status(mock_cred, mock_result)
        assert status == "ok"

    def test_status_expired(self):
        from dango.web.routes.variables import _derive_oauth_status

        mock_cred = MagicMock()
        mock_cred.is_expired.return_value = True
        mock_cred.is_expiring_soon.return_value = False

        mock_result = MagicMock()
        mock_result.valid = False
        mock_result.error_code = None

        status = _derive_oauth_status(mock_cred, mock_result)
        assert status == "expired"

    def test_status_expiring_soon(self):
        from dango.web.routes.variables import _derive_oauth_status

        mock_cred = MagicMock()
        mock_cred.is_expired.return_value = False
        mock_cred.is_expiring_soon.return_value = True

        mock_result = MagicMock()
        mock_result.valid = True
        mock_result.error_code = None

        status = _derive_oauth_status(mock_cred, mock_result)
        assert status == "expiring_soon"

    def test_status_unknown_on_network_error(self):
        from dango.web.routes.variables import _derive_oauth_status

        mock_cred = MagicMock()
        mock_cred.is_expired.return_value = False
        mock_cred.is_expiring_soon.return_value = False

        # Network error returns valid=True by design, but error_code is set
        mock_result = MagicMock()
        mock_result.valid = True
        mock_result.error_code = "network_error"

        status = _derive_oauth_status(mock_cred, mock_result)
        assert status == "unknown"


# ---------------------------------------------------------------------------
# Permission tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVariablesPage:
    """Tests for GET /settings/variables page route."""

    def test_admin_can_access_page(self, admin_client):
        client, _ = admin_client
        with patch("dango.web.routes.ui._render_template") as mock_render:
            mock_render.return_value = MagicMock()
            client.get("/settings/variables", headers=HEADERS)
            # Should not return 403 (permission check happens in route before render)
            # The mock prevents actual template rendering, so we check it was called
            mock_render.assert_called_once()

    def test_editor_denied_access_to_page(self, editor_client):
        client, _ = editor_client
        resp = client.get("/settings/variables", headers=HEADERS)
        assert resp.status_code == 403

    def test_editor_denied_access_to_api(self, editor_client):
        client, _ = editor_client
        resp = client.get("/api/variables", headers=HEADERS)
        assert resp.status_code == 403

    def test_editor_denied_post_to_api(self, editor_client):
        client, _ = editor_client
        resp = client.post("/api/variables", json={"key": "K", "value": "V"}, headers=HEADERS)
        assert resp.status_code == 403

    def test_editor_denied_delete_to_api(self, editor_client):
        client, _ = editor_client
        resp = client.delete("/api/variables/K", headers=HEADERS)
        assert resp.status_code == 403

    def test_editor_denied_oauth_status(self, editor_client):
        client, _ = editor_client
        resp = client.get("/api/variables/oauth", headers=HEADERS)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnvFileHelpers:
    """Tests for local _write_env_file and read_env_file functions."""

    def test_write_env_file_creates_file_with_correct_permissions(self, tmp_path):
        from dango.web.routes.variables import _write_env_file

        env_vars = {"KEY1": "value1", "KEY2": "value2"}
        _write_env_file(tmp_path, env_vars)

        env_file = tmp_path / ".env"
        assert env_file.exists()
        # Check permissions are 0o600
        import stat

        perms = stat.S_IMODE(env_file.stat().st_mode)
        assert perms == 0o600

    def test_write_and_read_roundtrip(self, tmp_path):
        from dango.web.routes.secrets import read_env_file
        from dango.web.routes.variables import _write_env_file

        original = {"KEY1": "value1", "KEY2": "value2"}
        _write_env_file(tmp_path, original)
        read_back = read_env_file(tmp_path)
        assert read_back == original
