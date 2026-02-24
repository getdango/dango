"""tests/unit/test_web_oauth_connect.py

Tests for dango.web.routes.oauth_connect — OAuth connect/callback routes.
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
from dango.web.routes.oauth_connect import router

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
    """Create a minimal FastAPI app with the oauth connect router."""
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
    """Create a test client with admin user injected via middleware."""
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
}


@pytest.fixture()
def setup(tmp_path: Path) -> tuple[TestClient, Path]:
    client, project_root = _setup_admin_client(tmp_path)
    return client, project_root


# ---------------------------------------------------------------------------
# GET /oauth/connect/{source_type} tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOAuthConnect:
    """Tests for GET /oauth/connect/{source_type}."""

    def test_unsupported_source_rejected(self, setup: tuple[TestClient, Path]) -> None:
        client, _tmp = setup
        resp = client.get(
            "/oauth/connect/unsupported_source",
            headers=HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "Unsupported" in resp.json()["message"]

    @patch("dango.web.routes.oauth_connect._load_cloud_domain", return_value=None)
    def test_no_domain_rejected(
        self, mock_domain: MagicMock, setup: tuple[TestClient, Path]
    ) -> None:
        client, _tmp = setup
        resp = client.get(
            "/oauth/connect/google_ads",
            headers=HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "domain" in resp.json()["message"].lower()

    @patch("dango.web.routes.oauth_connect._load_cloud_domain", return_value="example.com")
    @patch("dango.web.routes.oauth_connect._get_client_credentials", return_value=None)
    def test_no_credentials_rejected(
        self,
        mock_creds: MagicMock,
        mock_domain: MagicMock,
        setup: tuple[TestClient, Path],
    ) -> None:
        client, _tmp = setup
        resp = client.get(
            "/oauth/connect/google_ads",
            headers=HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "credentials" in resp.json()["message"].lower()

    @patch("dango.web.routes.oauth_connect._load_cloud_domain", return_value="example.com")
    @patch(
        "dango.web.routes.oauth_connect._get_client_credentials",
        return_value=("client-id", "client-secret"),
    )
    def test_google_redirect(
        self,
        mock_creds: MagicMock,
        mock_domain: MagicMock,
        setup: tuple[TestClient, Path],
    ) -> None:
        client, _tmp = setup
        resp = client.get(
            "/oauth/connect/google_ads",
            headers=HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "accounts.google.com" in location
        assert "client-id" in location
        # State cookies should be set
        cookies = resp.cookies
        assert "dango_source_oauth_state" in cookies
        assert "dango_source_oauth_type" in cookies

    @patch("dango.web.routes.oauth_connect._load_cloud_domain", return_value="example.com")
    @patch(
        "dango.web.routes.oauth_connect._get_client_credentials",
        return_value=("fb-app-id", "fb-secret"),
    )
    def test_facebook_redirect(
        self,
        mock_creds: MagicMock,
        mock_domain: MagicMock,
        setup: tuple[TestClient, Path],
    ) -> None:
        client, _tmp = setup
        resp = client.get(
            "/oauth/connect/facebook_ads",
            headers=HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "facebook.com" in location
        assert "fb-app-id" in location


# ---------------------------------------------------------------------------
# GET /oauth/callback/{source_type} tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOAuthCallback:
    """Tests for GET /oauth/callback/{source_type}."""

    def test_state_mismatch_redirects(self, setup: tuple[TestClient, Path]) -> None:
        client, _tmp = setup
        resp = client.get(
            "/oauth/callback/google_ads?state=wrong&code=abc",
            headers=HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 307 or resp.status_code == 302
        location = resp.headers.get("location", "")
        assert "error=state_mismatch" in location

    def test_provider_error_redirects(self, setup: tuple[TestClient, Path]) -> None:
        client, _tmp = setup
        # Set a matching state cookie
        client.cookies.set("dango_source_oauth_state", "test-state")
        client.cookies.set("dango_source_oauth_type", "google_ads")
        resp = client.get(
            "/oauth/callback/google_ads?state=test-state&error=access_denied",
            headers=HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 307 or resp.status_code == 302
        location = resp.headers.get("location", "")
        assert "error=provider_denied" in location

    def test_no_code_redirects(self, setup: tuple[TestClient, Path]) -> None:
        client, _tmp = setup
        client.cookies.set("dango_source_oauth_state", "test-state")
        client.cookies.set("dango_source_oauth_type", "google_ads")
        resp = client.get(
            "/oauth/callback/google_ads?state=test-state",
            headers=HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 307 or resp.status_code == 302
        location = resp.headers.get("location", "")
        assert "error=no_code" in location

    @patch("dango.web.routes.oauth_connect._load_cloud_domain", return_value="example.com")
    @patch(
        "dango.web.routes.oauth_connect._get_client_credentials",
        return_value=("client-id", "client-secret"),
    )
    @patch("dango.web.routes.oauth_connect.exchange_google_code")
    @patch("dango.web.routes.oauth_connect.fetch_google_user_info")
    @patch("dango.oauth.storage.OAuthStorage.save", return_value=True)
    @patch("dango.web.routes.oauth_connect.log_auth_event")
    def test_google_callback_success(
        self,
        mock_log: MagicMock,
        mock_save: MagicMock,
        mock_user_info: MagicMock,
        mock_exchange: MagicMock,
        mock_creds: MagicMock,
        mock_domain: MagicMock,
        setup: tuple[TestClient, Path],
    ) -> None:
        client, tmp_path = setup
        # Set up .dlt directory for OAuthStorage
        (tmp_path / ".dlt").mkdir()
        (tmp_path / ".dlt" / "secrets.toml").write_text("")

        mock_exchange.return_value = {
            "access_token": "at-123",
            "refresh_token": "rt-456",
            "expires_in": 3600,
        }
        mock_user_info.return_value = {
            "email": "user@example.com",
            "name": "Test User",
        }

        client.cookies.set("dango_source_oauth_state", "test-state")
        client.cookies.set("dango_source_oauth_type", "google_ads")
        resp = client.get(
            "/oauth/callback/google_ads?state=test-state&code=auth-code-123",
            headers=HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 307 or resp.status_code == 302
        location = resp.headers.get("location", "")
        assert "connected=google_ads" in location

        # Verify exchange was called
        mock_exchange.assert_called_once()
        # Verify audit event logged
        mock_log.assert_called_once()
        from dango.auth.audit import AuditEvent

        assert mock_log.call_args[0][0] == AuditEvent.OAUTH_SOURCE_CONNECTED

    @patch("dango.web.routes.oauth_connect._load_cloud_domain", return_value="example.com")
    @patch(
        "dango.web.routes.oauth_connect._get_client_credentials",
        return_value=("client-id", "client-secret"),
    )
    @patch("dango.web.routes.oauth_connect._exchange_code")
    def test_token_exchange_failure_redirects(
        self,
        mock_exchange: MagicMock,
        mock_creds: MagicMock,
        mock_domain: MagicMock,
        setup: tuple[TestClient, Path],
    ) -> None:
        client, _tmp = setup
        from dango.oauth.web_flow import OAuthFlowError

        mock_exchange.side_effect = OAuthFlowError("Token exchange failed", provider="google")

        client.cookies.set("dango_source_oauth_state", "test-state")
        client.cookies.set("dango_source_oauth_type", "google_ads")
        resp = client.get(
            "/oauth/callback/google_ads?state=test-state&code=bad-code",
            headers=HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 307 or resp.status_code == 302
        location = resp.headers.get("location", "")
        assert "error=token_exchange_failed" in location


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHelpers:
    """Tests for internal helper functions."""

    def test_get_redirect_uri(self) -> None:
        from dango.web.routes.oauth_connect import _get_redirect_uri

        uri = _get_redirect_uri("example.com", "google_ads")
        assert uri == "https://example.com/oauth/callback/google_ads"

    def test_get_client_credentials_google(self, tmp_path: Path) -> None:
        from dango.web.routes.oauth_connect import _get_client_credentials

        env_file = tmp_path / ".env"
        env_file.write_text("GOOGLE_CLIENT_ID=gid\nGOOGLE_CLIENT_SECRET=gsec\n")
        result = _get_client_credentials(tmp_path, "google")
        assert result == ("gid", "gsec")

    def test_get_client_credentials_facebook(self, tmp_path: Path) -> None:
        from dango.web.routes.oauth_connect import _get_client_credentials

        env_file = tmp_path / ".env"
        env_file.write_text("FACEBOOK_APP_ID=fid\nFACEBOOK_APP_SECRET=fsec\n")
        result = _get_client_credentials(tmp_path, "facebook")
        assert result == ("fid", "fsec")

    def test_get_client_credentials_missing(self, tmp_path: Path) -> None:
        from dango.web.routes.oauth_connect import _get_client_credentials

        result = _get_client_credentials(tmp_path, "google")
        assert result is None

    def test_get_client_credentials_unsupported_provider(self, tmp_path: Path) -> None:
        from dango.web.routes.oauth_connect import _get_client_credentials

        result = _get_client_credentials(tmp_path, "shopify")
        assert result is None

    def test_exchange_code_google(self) -> None:
        from dango.web.routes.oauth_connect import _exchange_code

        with patch("dango.web.routes.oauth_connect.exchange_google_code") as mock:
            mock.return_value = {"access_token": "at"}
            result = _exchange_code("google", "code", "cid", "cs", "uri")
            assert result == {"access_token": "at"}
            mock.assert_called_once()

    def test_exchange_code_facebook(self) -> None:
        from dango.web.routes.oauth_connect import _exchange_code

        with patch("dango.web.routes.oauth_connect.exchange_facebook_code") as mock:
            mock.return_value = {"access_token": "at"}
            result = _exchange_code("facebook", "code", "cid", "cs", "uri")
            assert result == {"access_token": "at"}
            mock.assert_called_once()

    def test_exchange_code_unsupported(self) -> None:
        from dango.oauth.web_flow import OAuthFlowError
        from dango.web.routes.oauth_connect import _exchange_code

        with pytest.raises(OAuthFlowError, match="Unsupported"):
            _exchange_code("shopify", "code", "cid", "cs", "uri")
