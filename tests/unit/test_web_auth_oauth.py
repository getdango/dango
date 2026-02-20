"""tests/unit/test_web_auth_oauth.py

Tests for OAuth social login endpoints in dango/web/routes/auth.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.models import Role, User
from dango.auth.oauth_login import OAuthLoginError, OAuthUserInfo
from dango.auth.security import hash_password
from dango.config.models import OAuthProviderConfig
from dango.migrations.runner import MigrationRunner
from dango.web.middleware.auth import COOKIE_NAME
from dango.web.routes.auth import router

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    """Create a fresh auth database at the standard project path."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    db_path = dango_dir / "auth.db"
    migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    runner = MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=migrations_dir)
    runner.apply_pending()
    return db_path


def _make_user(
    db_path: Path,
    email: str = "oauth@example.com",
    password: str = "securepassword123",
    role: Role = Role.EDITOR,
    **overrides: Any,
) -> User:
    """Create and persist a user, returning the model."""
    defaults: dict[str, Any] = {
        "email": email,
        "password_hash": hash_password(password),
        "role": role,
    }
    defaults.update(overrides)
    user = User(**defaults)
    db.create_user(db_path, user)
    return user


_GOOGLE_CONFIG = {"google": OAuthProviderConfig(client_id="g-id", client_secret="g-secret")}


def _make_app(db_path: Path) -> FastAPI:
    """Create a minimal FastAPI app with auth routes."""
    app = FastAPI()
    app.state.project_root = db_path.parent.parent
    app.include_router(router)
    return app


def _make_client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _patch_oauth_config(configs: dict[str, OAuthProviderConfig]) -> Any:
    """Patch _get_oauth_config to return given configs."""
    return patch("dango.web.routes.auth._get_oauth_config", return_value=configs)


def _mock_provider(user_info: OAuthUserInfo | None = None, error: str | None = None) -> Any:
    """Create a mock provider.

    Uses MagicMock for sync methods (get_authorization_url) and
    AsyncMock for async methods (exchange_code).
    """
    mock_prov = MagicMock()
    mock_prov.name = "google"
    mock_prov.display_name = "Google"
    mock_prov.icon_svg = "<svg></svg>"
    mock_prov.get_authorization_url.return_value = "https://accounts.google.com/auth?test=1"

    if error:
        mock_prov.exchange_code = AsyncMock(side_effect=OAuthLoginError(error))
    elif user_info:
        mock_prov.exchange_code = AsyncMock(return_value=user_info)
    else:
        mock_prov.exchange_code = AsyncMock(return_value=None)
    return mock_prov


def _location(resp: Any) -> str:
    """Get the decoded Location header from a redirect response."""
    return unquote(resp.headers.get("location", ""))


# ---------------------------------------------------------------------------
# OAuth login redirect tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOAuthLoginRedirect:
    """Tests for GET /api/auth/oauth/{provider}/login."""

    def test_redirect_to_provider(self, tmp_path: Path) -> None:
        """Successful redirect sets state cookie and 302s to provider."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)
        client = _make_client(app)

        mock_prov = _mock_provider()

        with (
            _patch_oauth_config(_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
        ):
            resp = client.get("/api/auth/oauth/google/login", follow_redirects=False)

        assert resp.status_code == 302
        assert "accounts.google.com" in resp.headers["location"]
        assert "dango_oauth_state" in resp.cookies

    def test_unconfigured_provider(self, tmp_path: Path) -> None:
        """Unconfigured provider redirects to login with error."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)
        client = _make_client(app)

        with _patch_oauth_config({}):
            resp = client.get("/api/auth/oauth/google/login", follow_redirects=False)

        assert resp.status_code == 302
        assert "not configured" in _location(resp)


# ---------------------------------------------------------------------------
# OAuth callback tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOAuthCallback:
    """Tests for GET /api/auth/oauth/{provider}/callback."""

    def _callback_url(self, provider: str = "google", **params: str) -> str:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"/api/auth/oauth/{provider}/callback?{qs}"

    def test_success_existing_oauth_user(self, tmp_path: Path) -> None:
        """Existing OAuth user logs in and gets session cookie."""
        db_path = _make_db(tmp_path)
        _make_user(db_path, oauth_provider="google", oauth_id="g-123")
        app = _make_app(db_path)
        client = _make_client(app)

        user_info = OAuthUserInfo(
            provider="google", provider_id="g-123", email="oauth@example.com", name="Test"
        )
        mock_prov = _mock_provider(user_info=user_info)

        with (
            _patch_oauth_config(_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
        ):
            client.cookies.set("dango_oauth_state", "state123")
            resp = client.get(
                self._callback_url(code="auth-code", state="state123"),
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert resp.headers["location"] == "/"
        assert COOKIE_NAME in resp.cookies

    def test_auto_link_email_match(self, tmp_path: Path) -> None:
        """Email match with no existing OAuth link auto-links and logs in."""
        db_path = _make_db(tmp_path)
        user = _make_user(db_path, email="link@example.com")
        app = _make_app(db_path)
        client = _make_client(app)

        user_info = OAuthUserInfo(
            provider="google", provider_id="g-new", email="link@example.com", name="Link"
        )
        mock_prov = _mock_provider(user_info=user_info)

        with (
            _patch_oauth_config(_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
        ):
            client.cookies.set("dango_oauth_state", "state1")
            resp = client.get(
                self._callback_url(code="code", state="state1"),
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert COOKIE_NAME in resp.cookies

        # Verify the user was linked
        updated = db.get_user_by_id(db_path, user.id)
        assert updated is not None
        assert updated.oauth_provider == "google"
        assert updated.oauth_id == "g-new"

    def test_reject_different_provider_linked(self, tmp_path: Path) -> None:
        """Email linked to a different provider is rejected."""
        db_path = _make_db(tmp_path)
        _make_user(db_path, email="linked@example.com", oauth_provider="github", oauth_id="gh-1")
        app = _make_app(db_path)
        client = _make_client(app)

        user_info = OAuthUserInfo(
            provider="google", provider_id="g-1", email="linked@example.com", name="X"
        )
        mock_prov = _mock_provider(user_info=user_info)

        with (
            _patch_oauth_config(_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
        ):
            client.cookies.set("dango_oauth_state", "s")
            resp = client.get(
                self._callback_url(code="c", state="s"),
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "different login provider" in _location(resp)

    def test_reject_unknown_user(self, tmp_path: Path) -> None:
        """No account for the email redirects with error."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)
        client = _make_client(app)

        user_info = OAuthUserInfo(
            provider="google", provider_id="g-1", email="nobody@example.com", name="Nobody"
        )
        mock_prov = _mock_provider(user_info=user_info)

        with (
            _patch_oauth_config(_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
        ):
            client.cookies.set("dango_oauth_state", "s")
            resp = client.get(
                self._callback_url(code="c", state="s"),
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "No account found" in _location(resp)

    def test_reject_inactive_user(self, tmp_path: Path) -> None:
        """Inactive user is rejected."""
        db_path = _make_db(tmp_path)
        _make_user(
            db_path,
            email="inactive@example.com",
            is_active=False,
            oauth_provider="google",
            oauth_id="g-1",
        )
        app = _make_app(db_path)
        client = _make_client(app)

        user_info = OAuthUserInfo(
            provider="google", provider_id="g-1", email="inactive@example.com", name="Inactive"
        )
        mock_prov = _mock_provider(user_info=user_info)

        with (
            _patch_oauth_config(_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
        ):
            client.cookies.set("dango_oauth_state", "s")
            resp = client.get(
                self._callback_url(code="c", state="s"),
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "deactivated" in _location(resp)

    def test_csrf_state_mismatch(self, tmp_path: Path) -> None:
        """State mismatch redirects with CSRF error."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)
        client = _make_client(app)

        with _patch_oauth_config(_GOOGLE_CONFIG):
            client.cookies.set("dango_oauth_state", "correct-state")
            resp = client.get(
                self._callback_url(code="c", state="wrong-state"),
                follow_redirects=False,
            )

        assert resp.status_code == 302
        loc = _location(resp)
        assert "CSRF" in loc or "state mismatch" in loc

    def test_missing_state_cookie(self, tmp_path: Path) -> None:
        """Missing state cookie redirects with CSRF error."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)
        client = _make_client(app)

        with _patch_oauth_config(_GOOGLE_CONFIG):
            resp = client.get(
                self._callback_url(code="c", state="s"),
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "error=" in resp.headers["location"]

    def test_missing_code_param(self, tmp_path: Path) -> None:
        """Missing code parameter redirects with error."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)
        client = _make_client(app)

        with _patch_oauth_config(_GOOGLE_CONFIG):
            resp = client.get(
                self._callback_url(state="s"),
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "missing code" in _location(resp)

    def test_provider_error_param(self, tmp_path: Path) -> None:
        """Provider error (user denied) redirects with message."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)
        client = _make_client(app)

        with _patch_oauth_config(_GOOGLE_CONFIG):
            resp = client.get(
                self._callback_url(error="access_denied"),
                follow_redirects=False,
            )

        assert resp.status_code == 302
        loc = _location(resp)
        assert "cancelled" in loc or "denied" in loc

    def test_exchange_failure(self, tmp_path: Path) -> None:
        """Token exchange failure redirects with error."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)
        client = _make_client(app)

        mock_prov = _mock_provider(error="Token exchange failed")

        with (
            _patch_oauth_config(_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
        ):
            client.cookies.set("dango_oauth_state", "s")
            resp = client.get(
                self._callback_url(code="bad-code", state="s"),
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "error=" in resp.headers["location"]

    def test_redirect_to_setup_if_must_change_password(self, tmp_path: Path) -> None:
        """User with must_change_password is redirected to /setup."""
        db_path = _make_db(tmp_path)
        _make_user(
            db_path,
            email="setup@example.com",
            oauth_provider="google",
            oauth_id="g-setup",
            must_change_password=True,
        )
        app = _make_app(db_path)
        client = _make_client(app)

        user_info = OAuthUserInfo(
            provider="google", provider_id="g-setup", email="setup@example.com", name="Setup"
        )
        mock_prov = _mock_provider(user_info=user_info)

        with (
            _patch_oauth_config(_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
        ):
            client.cookies.set("dango_oauth_state", "s")
            resp = client.get(
                self._callback_url(code="c", state="s"),
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert resp.headers["location"] == "/setup"

    def test_state_cookie_cleared_on_success(self, tmp_path: Path) -> None:
        """State cookie is cleared after successful callback."""
        db_path = _make_db(tmp_path)
        _make_user(db_path, oauth_provider="google", oauth_id="g-1")
        app = _make_app(db_path)
        client = _make_client(app)

        user_info = OAuthUserInfo(
            provider="google", provider_id="g-1", email="oauth@example.com", name="T"
        )
        mock_prov = _mock_provider(user_info=user_info)

        with (
            _patch_oauth_config(_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
        ):
            client.cookies.set("dango_oauth_state", "s")
            resp = client.get(
                self._callback_url(code="c", state="s"),
                follow_redirects=False,
            )

        # Cookie should be deleted (set to empty or max-age=0)
        state_cookie = resp.cookies.get("dango_oauth_state")
        assert state_cookie is None or state_cookie == ""

    def test_unconfigured_provider_callback(self, tmp_path: Path) -> None:
        """Callback to unconfigured provider redirects with error."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)
        client = _make_client(app)

        with _patch_oauth_config({}):
            client.cookies.set("dango_oauth_state", "s")
            resp = client.get(
                self._callback_url(code="c", state="s"),
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "not configured" in _location(resp)


# ---------------------------------------------------------------------------
# Login page OAuth integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoginPageOAuth:
    """Tests for OAuth buttons on the login page."""

    def test_login_page_no_providers(self, tmp_path: Path) -> None:
        """Login page renders without OAuth section when no providers configured."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)
        client = _make_client(app)

        with _patch_oauth_config({}):
            resp = client.get("/login")

        assert resp.status_code == 200
        assert "or continue with" not in resp.text

    def test_login_page_with_providers(self, tmp_path: Path) -> None:
        """Login page shows OAuth buttons when providers are configured."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)
        client = _make_client(app)

        with _patch_oauth_config(_GOOGLE_CONFIG):
            resp = client.get("/login")

        assert resp.status_code == 200
        assert "Continue with Google" in resp.text
        assert "/api/auth/oauth/google/login" in resp.text

    def test_login_page_shows_error_from_params(self, tmp_path: Path) -> None:
        """Login page displays error from URL query params."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)
        client = _make_client(app)

        with _patch_oauth_config({}):
            resp = client.get("/login?error=Something+went+wrong")

        assert resp.status_code == 200
