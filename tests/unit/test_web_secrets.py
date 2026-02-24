"""tests/unit/test_web_secrets.py

Tests for dango.web.routes.secrets — secrets management API endpoints.
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
from dango.web.routes.secrets import router

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _make_admin() -> User:
    """Create an admin user for tests."""
    return User(
        id="u-admin-1",
        email="admin@test.com",
        password_hash="hashed",
        role=Role.ADMIN,
        is_active=True,
    )


def _make_app(project_root: Path) -> FastAPI:
    """Create a minimal FastAPI app with the secrets router."""
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


def _setup_admin_client(tmp_path: Path) -> tuple[TestClient, Path]:
    """Create a test client with an admin user injected via middleware."""
    admin = _make_admin()
    app = _make_app(tmp_path)

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = admin
        request.state.auth_method = "session"
        return await call_next(request)

    client = TestClient(app, raise_server_exceptions=False)
    return client, tmp_path


HEADERS: dict[str, str] = {
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/json",
}


@pytest.fixture()
def setup(tmp_path):
    client, project_root = _setup_admin_client(tmp_path)
    return client, project_root


# ---------------------------------------------------------------------------
# .env CRUD tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListSecrets:
    """Tests for GET /api/secrets."""

    def test_empty_list(self, setup):
        client, tmp_path = setup
        resp = client.get("/api/secrets", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["env_vars"] == []

    def test_list_with_env_vars(self, setup):
        client, tmp_path = setup
        (tmp_path / ".env").write_text("FOO=bar\nBAZ=qux\n")
        resp = client.get("/api/secrets", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["env_vars"]) == 2
        keys = [v["key"] for v in data["env_vars"]]
        assert "FOO" in keys
        assert "BAZ" in keys
        for v in data["env_vars"]:
            assert v["masked_value"] == "***"


@pytest.mark.unit
class TestSetSecret:
    """Tests for POST /api/secrets."""

    def test_set_new_variable(self, setup):
        client, tmp_path = setup
        resp = client.post(
            "/api/secrets", json={"key": "NEW_KEY", "value": "new_value"}, headers=HEADERS
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "created" in data["message"]
        content = (tmp_path / ".env").read_text()
        assert "NEW_KEY=new_value" in content

    def test_update_existing_variable(self, setup):
        client, tmp_path = setup
        (tmp_path / ".env").write_text("KEY=old\n")
        resp = client.post("/api/secrets", json={"key": "KEY", "value": "new"}, headers=HEADERS)
        assert resp.status_code == 200
        assert "updated" in resp.json()["message"]
        content = (tmp_path / ".env").read_text()
        assert "KEY=new" in content

    def test_empty_key_rejected(self, setup):
        client, _tmp = setup
        resp = client.post("/api/secrets", json={"key": "", "value": "v"}, headers=HEADERS)
        assert resp.status_code == 400

    def test_missing_key_rejected(self, setup):
        client, _tmp = setup
        resp = client.post("/api/secrets", json={"value": "v"}, headers=HEADERS)
        assert resp.status_code == 400

    @patch("dango.web.routes.secrets.log_auth_event")
    def test_audit_event_logged(self, mock_log, setup):
        client, _tmp = setup
        client.post("/api/secrets", json={"key": "K", "value": "V"}, headers=HEADERS)
        mock_log.assert_called_once()
        from dango.auth.audit import AuditEvent

        assert mock_log.call_args[0][0] == AuditEvent.SECRET_SET


@pytest.mark.unit
class TestDeleteSecret:
    """Tests for DELETE /api/secrets/{key}."""

    def test_delete_existing_key(self, setup):
        client, tmp_path = setup
        (tmp_path / ".env").write_text("FOO=bar\nBAZ=qux\n")
        resp = client.delete("/api/secrets/FOO", headers=HEADERS)
        assert resp.status_code == 200
        content = (tmp_path / ".env").read_text()
        assert "FOO" not in content
        assert "BAZ=qux" in content

    def test_delete_missing_key(self, setup):
        client, _tmp = setup
        resp = client.delete("/api/secrets/MISSING", headers=HEADERS)
        assert resp.status_code == 404

    @patch("dango.web.routes.secrets.log_auth_event")
    def test_audit_event_logged(self, mock_log, setup):
        client, tmp_path = setup
        (tmp_path / ".env").write_text("K=V\n")
        client.delete("/api/secrets/K", headers=HEADERS)
        mock_log.assert_called_once()
        from dango.auth.audit import AuditEvent

        assert mock_log.call_args[0][0] == AuditEvent.SECRET_DELETED


# ---------------------------------------------------------------------------
# OAuth credential tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListOAuthCredentials:
    """Tests for GET /api/secrets/oauth."""

    def test_empty_list(self, setup):
        client, tmp_path = setup
        # OAuthStorage needs .dlt dir
        (tmp_path / ".dlt").mkdir()
        (tmp_path / ".dlt" / "secrets.toml").write_text("")
        resp = client.get("/api/secrets/oauth", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json() == []


@pytest.mark.unit
class TestDisconnectOAuth:
    """Tests for DELETE /api/secrets/oauth/{source_type}."""

    def test_disconnect_nonexistent(self, setup):
        client, tmp_path = setup
        (tmp_path / ".dlt").mkdir()
        (tmp_path / ".dlt" / "secrets.toml").write_text("")
        resp = client.delete("/api/secrets/oauth/google_ads", headers=HEADERS)
        assert resp.status_code == 404

    @patch("dango.web.routes.secrets.log_auth_event")
    def test_audit_on_disconnect(self, mock_log, setup):
        client, tmp_path = setup
        (tmp_path / ".dlt").mkdir()
        (tmp_path / ".dlt" / "secrets.toml").write_text("")

        with (
            patch("dango.oauth.storage.OAuthStorage.exists", return_value=True),
            patch("dango.oauth.storage.OAuthStorage.delete", return_value=True),
        ):
            resp = client.delete("/api/secrets/oauth/google_ads", headers=HEADERS)

        assert resp.status_code == 200
        mock_log.assert_called_once()
        from dango.auth.audit import AuditEvent

        assert mock_log.call_args[0][0] == AuditEvent.OAUTH_SOURCE_DISCONNECTED


# ---------------------------------------------------------------------------
# .env file helpers (unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnvFileHelpers:
    """Tests for _read_env_file and _write_env_file."""

    def test_read_nonexistent_returns_empty(self, tmp_path):
        from dango.web.routes.secrets import _read_env_file

        assert _read_env_file(tmp_path) == {}

    def test_write_creates_file(self, tmp_path):
        from dango.web.routes.secrets import _write_env_file

        _write_env_file(tmp_path, {"KEY": "VALUE"})
        env_file = tmp_path / ".env"
        assert env_file.exists()
        mode = env_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_roundtrip(self, tmp_path):
        from dango.web.routes.secrets import _read_env_file, _write_env_file

        original = {"FOO": "bar", "BAZ": "qux"}
        _write_env_file(tmp_path, original)
        result = _read_env_file(tmp_path)
        assert result == original

    def test_file_permissions_600(self, tmp_path):
        from dango.web.routes.secrets import _write_env_file

        _write_env_file(tmp_path, {"A": "1"})
        env_file = tmp_path / ".env"
        mode = env_file.stat().st_mode & 0o777
        assert mode == 0o600
