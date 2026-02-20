"""tests/integration/test_auth_api.py

Integration tests for auth API endpoints exercising the full stack:
TestClient → AuthMiddleware → route handler → real SQLite DB → response.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pyotp
import pytest
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.models import UserUpdate
from dango.auth.oauth_login import OAuthUserInfo
from dango.auth.security import generate_invite_token
from dango.auth.totp import enable_totp, setup_totp
from dango.config.models import AuthConfig, OAuthProviderConfig
from dango.web.middleware.auth import COOKIE_NAME, AuthMiddleware
from tests.integration.conftest import auth_headers, login_user, make_test_user

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_2fa_user(
    db_path: Path,
    email: str = "2fa@example.com",
    password: str = "securepassword123",
) -> tuple[Any, str]:
    """Create a user with TOTP enabled. Returns (user, totp_secret)."""
    user = make_test_user(db_path, email=email, password=password)
    secret = pyotp.random_base32()
    from dango.auth.security import generate_recovery_codes

    codes = generate_recovery_codes()
    setup_totp(db_path, user.id, secret, codes)
    enable_totp(db_path, user.id)
    return user, secret


def _mock_provider(user_info: OAuthUserInfo) -> MagicMock:
    """Create a mock OAuth provider that returns user_info on exchange_code."""
    mock_prov = MagicMock()
    mock_prov.name = "google"
    mock_prov.display_name = "Google"
    mock_prov.icon_svg = "<svg></svg>"
    mock_prov.get_authorization_url.return_value = "https://accounts.google.com/auth?test=1"
    mock_prov.exchange_code = AsyncMock(return_value=user_info)
    return mock_prov


_GOOGLE_CONFIG = {"google": OAuthProviderConfig(client_id="g-id", client_secret="g-secret")}


def _clear_middleware_cache(app: Any) -> None:
    """Walk the ASGI middleware stack to clear AuthMiddleware's toggle cache."""
    current = getattr(app, "middleware_stack", None)
    while current is not None:
        if isinstance(current, AuthMiddleware):
            current._auth_enabled_cache = None
            current._cache_time = 0.0
            return
        current = getattr(current, "app", None)
    raise AssertionError("AuthMiddleware not found in middleware stack")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAuthAPI:
    """Integration tests for auth API endpoints with real middleware + DB."""

    def test_login_success_cookie_round_trip(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """Login → cookie set → GET /api/auth/me with cookie → 200."""
        make_test_user(auth_db_path, email="user@example.com")
        resp = login_user(auth_client, "user@example.com")
        assert resp.status_code == 200
        assert COOKIE_NAME in resp.cookies

        # Use the session cookie for /me
        me_resp = auth_client.get("/api/auth/me", headers=auth_headers(auth_client))
        assert me_resp.status_code == 200
        data = me_resp.json()
        assert data["email"] == "user@example.com"

    def test_login_bad_password(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """Wrong password → 400, no session cookie."""
        make_test_user(auth_db_path, email="user@example.com")
        resp = login_user(auth_client, "user@example.com", "wrongpassword1")
        assert resp.status_code == 400
        assert COOKIE_NAME not in resp.cookies

    def test_login_unknown_email(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """Unknown email → 400 (timing-safe, no enumeration)."""
        resp = login_user(auth_client, "nobody@example.com", "somepassword1")
        assert resp.status_code == 400
        assert resp.json()["message"] == "Invalid email or password"

    def test_login_inactive_user(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """Deactivated user → 400."""
        make_test_user(auth_db_path, email="inactive@example.com", is_active=False)
        resp = login_user(auth_client, "inactive@example.com")
        assert resp.status_code == 400
        assert resp.json()["message"] == "Invalid email or password"

    def test_login_locked_account(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """5 failed attempts → 423 Locked."""
        make_test_user(auth_db_path, email="locked@example.com")
        for _ in range(5):
            login_user(auth_client, "locked@example.com", "wrongpassword1")

        resp = login_user(auth_client, "locked@example.com", "wrongpassword1")
        assert resp.status_code == 423
        assert "locked" in resp.json()["message"].lower()

    def test_login_2fa_full_flow(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """Login (2FA) → partial cookie → verify TOTP → full session → /me works."""
        _user, secret = _setup_2fa_user(auth_db_path)

        # Step 1: Login → partial session
        resp = login_user(auth_client, "2fa@example.com")
        assert resp.status_code == 200
        assert resp.json()["requires_2fa"] is True
        partial_cookie = resp.cookies.get(COOKIE_NAME)
        assert partial_cookie is not None

        # Step 2: Verify TOTP code (2fa/verify is public, pass partial cookie)
        totp = pyotp.TOTP(secret)
        code = totp.now()
        verify_resp = auth_client.post(
            "/api/auth/2fa/verify",
            json={"code": code},
            headers=auth_headers(cookie=partial_cookie),
        )
        assert verify_resp.status_code == 200
        assert "user" in verify_resp.json()
        full_cookie = verify_resp.cookies.get(COOKIE_NAME)
        assert full_cookie is not None

        # Step 3: Full session works for /me
        me_resp = auth_client.get("/api/auth/me", headers=auth_headers(cookie=full_cookie))
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == "2fa@example.com"

    def test_logout_invalidates_session(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """Login → logout → /me returns 401."""
        make_test_user(auth_db_path, email="logout@example.com")
        login_user(auth_client, "logout@example.com")

        # Logout
        hdrs = auth_headers(auth_client)
        logout_resp = auth_client.post("/api/auth/logout", headers=hdrs)
        assert logout_resp.status_code == 200

        # Session is gone — /me should fail (same cookie is now invalid)
        me_resp = auth_client.get("/api/auth/me", headers=hdrs)
        assert me_resp.status_code in (401, 302)

    def test_api_key_lifecycle(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """Create API key → use Bearer → revoke → Bearer fails."""
        make_test_user(auth_db_path, email="apikey@example.com")
        login_user(auth_client, "apikey@example.com")

        # Create API key
        create_resp = auth_client.post(
            "/api/auth/api-keys",
            json={"name": "test-key"},
            headers=auth_headers(auth_client),
        )
        assert create_resp.status_code == 200
        key_data = create_resp.json()
        raw_key = key_data["key"]
        key_id = key_data["id"]

        # Use Bearer token (new client to avoid cookies)
        bearer_client = TestClient(auth_client.app, raise_server_exceptions=False)
        bearer_resp = bearer_client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert bearer_resp.status_code == 200
        assert bearer_resp.json()["email"] == "apikey@example.com"

        # Revoke the key
        revoke_resp = auth_client.request(
            "DELETE",
            f"/api/auth/api-keys/{key_id}",
            headers=auth_headers(auth_client),
        )
        assert revoke_resp.status_code == 200

        # Bearer should now fail
        fail_resp = bearer_client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert fail_resp.status_code in (401, 302)

    def test_password_change_invalidates_old_session(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """Login → change password → original cookie → 401."""
        make_test_user(auth_db_path, email="pwchange@example.com")
        login_resp = login_user(auth_client, "pwchange@example.com")
        old_cookie = login_resp.cookies.get(COOKIE_NAME)
        assert old_cookie is not None

        # Change password
        change_resp = auth_client.post(
            "/api/auth/change-password",
            json={
                "current_password": "securepassword123",
                "new_password": "newsecurepassword456",
            },
            headers=auth_headers(auth_client),
        )
        assert change_resp.status_code == 200

        # Old cookie should be invalid
        me_resp = auth_client.get("/api/auth/me", headers=auth_headers(cookie=old_cookie))
        assert me_resp.status_code in (401, 302)

    def test_auth_disabled_bypasses_middleware(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """With auth disabled, middleware passes through with user=None."""
        # Disable auth
        auth_yml = auth_db_path.parent / "auth.yml"
        auth_yml.write_text("enabled: false\n")

        # Make a request first to force middleware_stack to be built
        auth_client.get("/api/health")

        # Walk the ASGI middleware stack to clear auth toggle cache
        _clear_middleware_cache(auth_client.app)

        # Request without credentials — middleware should pass through
        resp = auth_client.get("/api/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        # When auth is disabled, /me returns auth_enabled: False
        assert data.get("auth_enabled") is False

    def test_oauth_callback_creates_session(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """OAuth callback → session cookie set (mock external HTTP)."""
        make_test_user(
            auth_db_path,
            email="oauth@example.com",
            oauth_provider="google",
            oauth_id="g-123",
        )

        user_info = OAuthUserInfo(
            provider="google", provider_id="g-123", email="oauth@example.com", name="Test"
        )
        mock_prov = _mock_provider(user_info)

        with (
            patch("dango.web.routes.auth._get_oauth_config", return_value=_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
        ):
            resp = auth_client.get(
                "/api/auth/oauth/google/callback?code=auth-code&state=state123",
                headers={"Cookie": "dango_oauth_state=state123"},
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert resp.headers["location"] == "/"
        assert COOKIE_NAME in resp.cookies

    def test_oauth_callback_respects_2fa(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """OAuth callback for 2FA user → redirect to login?requires_2fa + partial session."""
        user, _secret = _setup_2fa_user(
            auth_db_path,
            email="oauth2fa@example.com",
        )
        # Link OAuth
        db.update_user(auth_db_path, user.id, UserUpdate(oauth_provider="google", oauth_id="g-2fa"))

        user_info = OAuthUserInfo(
            provider="google",
            provider_id="g-2fa",
            email="oauth2fa@example.com",
            name="2FA OAuth",
        )
        mock_prov = _mock_provider(user_info)

        with (
            patch("dango.web.routes.auth._get_oauth_config", return_value=_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
        ):
            resp = auth_client.get(
                "/api/auth/oauth/google/callback?code=auth-code&state=state456",
                headers={"Cookie": "dango_oauth_state=state456"},
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "requires_2fa=true" in resp.headers["location"]
        assert COOKIE_NAME in resp.cookies

        # Partial session must NOT access protected endpoints
        partial_cookie = resp.cookies.get(COOKIE_NAME)
        me_resp = auth_client.get(
            "/api/auth/me",
            headers=auth_headers(cookie=partial_cookie),
        )
        assert me_resp.status_code in (401, 302)

    def test_oauth_callback_require_2fa_setup_redirect(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """OAuth callback when require_2fa=True but user has no TOTP → redirect to /setup."""
        make_test_user(
            auth_db_path,
            email="setup2fa@example.com",
            oauth_provider="google",
            oauth_id="g-setup",
        )

        user_info = OAuthUserInfo(
            provider="google", provider_id="g-setup", email="setup2fa@example.com", name="Setup"
        )
        mock_prov = _mock_provider(user_info)
        require_2fa_config = AuthConfig(enabled=True, require_2fa=True)

        with (
            patch("dango.web.routes.auth._get_oauth_config", return_value=_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
            patch("dango.web.routes.auth._get_auth_config", return_value=require_2fa_config),
            patch(
                "dango.auth.metabase_bridge.bridge_metabase_login",
                new_callable=AsyncMock,
                return_value="mb-setup-token",
            ) as mock_bridge,
        ):
            resp = auth_client.get(
                "/api/auth/oauth/google/callback?code=auth-code&state=state-setup",
                headers={"Cookie": "dango_oauth_state=state-setup"},
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "requires_2fa_setup=true" in resp.headers["location"]
        assert COOKIE_NAME in resp.cookies
        # Full session (not partial) — so Metabase bridge should be called
        assert resp.cookies.get("metabase.SESSION") == "mb-setup-token"
        mock_bridge.assert_called_once()

    def test_invite_accept_then_login(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """Create invited user → accept invite → login → authenticated."""
        raw_token, token_hash = generate_invite_token()
        make_test_user(
            auth_db_path,
            email="invited@example.com",
            password_hash=None,
            invite_token_hash=token_hash,
            invite_expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )

        # Accept invite (public endpoint)
        accept_resp = auth_client.post(
            "/api/auth/accept-invite",
            json={"token": raw_token, "password": "invitepassword123"},
        )
        assert accept_resp.status_code == 200

        # Login with the new password
        login_resp = login_user(auth_client, "invited@example.com", "invitepassword123")
        assert login_resp.status_code == 200
        assert COOKIE_NAME in login_resp.cookies

        # Confirm authenticated
        me_resp = auth_client.get("/api/auth/me", headers=auth_headers(auth_client))
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == "invited@example.com"

    def test_invite_accept_expired_token(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """Accept invite with expired token → 400."""
        raw_token, token_hash = generate_invite_token()
        make_test_user(
            auth_db_path,
            email="expired@example.com",
            password_hash=None,
            invite_token_hash=token_hash,
            invite_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        resp = auth_client.post(
            "/api/auth/accept-invite",
            json={"token": raw_token, "password": "newpassword12345"},
        )
        assert resp.status_code == 400
        assert "invalid or has expired" in resp.json()["message"].lower()

    def test_oauth_callback_bridges_metabase_session(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """OAuth callback sets metabase.SESSION cookie when bridge succeeds."""
        make_test_user(
            auth_db_path,
            email="mb-oauth@example.com",
            oauth_provider="google",
            oauth_id="g-mb",
        )

        user_info = OAuthUserInfo(
            provider="google", provider_id="g-mb", email="mb-oauth@example.com", name="Test"
        )
        mock_prov = _mock_provider(user_info)

        with (
            patch("dango.web.routes.auth._get_oauth_config", return_value=_GOOGLE_CONFIG),
            patch("dango.web.routes.auth.get_provider", return_value=mock_prov),
            patch(
                "dango.auth.metabase_bridge.bridge_metabase_login",
                new_callable=AsyncMock,
                return_value="mb-session-token",
            ) as mock_bridge,
        ):
            resp = auth_client.get(
                "/api/auth/oauth/google/callback?code=auth-code&state=state-mb",
                headers={"Cookie": "dango_oauth_state=state-mb"},
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert COOKIE_NAME in resp.cookies
        assert resp.cookies.get("metabase.SESSION") == "mb-session-token"
        mock_bridge.assert_called_once()

    def test_2fa_verify_bridges_metabase_session(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """2FA verify sets metabase.SESSION cookie when bridge succeeds."""
        _user, secret = _setup_2fa_user(auth_db_path)

        # Step 1: Login → partial session
        resp = login_user(auth_client, "2fa@example.com")
        assert resp.json()["requires_2fa"] is True
        partial_cookie = resp.cookies.get(COOKIE_NAME)

        # Step 2: Verify TOTP with Metabase bridge mocked
        totp = pyotp.TOTP(secret)
        code = totp.now()
        with patch(
            "dango.auth.metabase_bridge.bridge_metabase_login",
            new_callable=AsyncMock,
            return_value="mb-2fa-token",
        ) as mock_bridge:
            verify_resp = auth_client.post(
                "/api/auth/2fa/verify",
                json={"code": code},
                headers=auth_headers(cookie=partial_cookie),
            )

        assert verify_resp.status_code == 200
        assert verify_resp.cookies.get("metabase.SESSION") == "mb-2fa-token"
        mock_bridge.assert_called_once()
