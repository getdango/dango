"""tests/integration/test_auth_middleware_integration.py

Integration tests for auth middleware behavior exercising the full stack:
CSRF enforcement, role-based permission checks, public route bypass,
session lifecycle, and rate limiting through real middleware.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.auth.models import Role
from dango.auth.security import generate_invite_token
from dango.config.models import RateLimitConfig, RateLimitGroupConfig
from dango.web.middleware.auth import COOKIE_NAME, AuthMiddleware
from dango.web.middleware.rate_limit import RateLimitMiddleware
from tests.integration.conftest import auth_headers, login_user, make_test_user

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCSRFEnforcement:
    """CSRF enforcement through real middleware."""

    def test_csrf_rejected_cookie_auth_post(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """POST with cookie auth but no X-Requested-With → 403 CSRF."""
        make_test_user(auth_db_path, email="csrf@example.com")
        login_user(auth_client, "csrf@example.com")
        cookie = getattr(auth_client, "_session_cookie", None)
        assert cookie is not None

        # POST with cookie but no CSRF header
        resp = auth_client.post(
            "/api/auth/api-keys",
            json={"name": "test-key"},
            headers={"Cookie": f"{COOKIE_NAME}={cookie}"},
        )
        assert resp.status_code == 403
        assert "csrf" in resp.json()["message"].lower()

    def test_csrf_allowed_for_api_key_auth(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """POST with Bearer token (no CSRF header) → passes CSRF check."""
        make_test_user(auth_db_path, email="bearer@example.com")
        login_user(auth_client, "bearer@example.com")

        # Create API key via cookie auth (with CSRF header)
        create_resp = auth_client.post(
            "/api/auth/api-keys",
            json={"name": "test-key"},
            headers=auth_headers(auth_client),
        )
        assert create_resp.status_code == 200
        raw_key = create_resp.json()["key"]

        # Use Bearer token without CSRF header on a new client
        bearer_client = TestClient(auth_client.app, raise_server_exceptions=False)
        resp = bearer_client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 200

    def test_csrf_not_required_for_get(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """GET with cookie auth (no X-Requested-With) → 200."""
        make_test_user(auth_db_path, email="getuser@example.com")
        login_user(auth_client, "getuser@example.com")
        cookie = getattr(auth_client, "_session_cookie", None)

        # GET without CSRF header should work (safe method)
        resp = auth_client.get(
            "/api/auth/me",
            headers={"Cookie": f"{COOKIE_NAME}={cookie}"},
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "getuser@example.com"


@pytest.mark.integration
class TestRolePermissions:
    """Role-based access control through real middleware + require_permission."""

    def test_admin_can_access_admin_endpoints(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """Admin → GET /api/admin/users → 200."""
        make_test_user(auth_db_path, email="admin@example.com", role=Role.ADMIN)
        login_user(auth_client, "admin@example.com")

        resp = auth_client.get("/api/admin/users", headers=auth_headers(auth_client))
        assert resp.status_code == 200

    def test_viewer_rejected_from_admin_endpoints(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """Viewer → GET /api/admin/users → 403."""
        make_test_user(auth_db_path, email="viewer@example.com", role=Role.VIEWER)
        login_user(auth_client, "viewer@example.com")

        resp = auth_client.get("/api/admin/users", headers=auth_headers(auth_client))
        assert resp.status_code == 403

    def test_editor_rejected_from_admin_endpoints(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """Editor → POST /api/admin/users → 403 (users.manage not in Editor)."""
        make_test_user(auth_db_path, email="editor@example.com", role=Role.EDITOR)
        login_user(auth_client, "editor@example.com")

        resp = auth_client.post(
            "/api/admin/users",
            json={"email": "newuser@example.com", "role": "viewer"},
            headers=auth_headers(auth_client),
        )
        assert resp.status_code == 403


@pytest.mark.integration
class TestPublicRoutes:
    """Public routes bypass auth middleware."""

    def test_public_routes_bypass_auth(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """Public routes accessible without authentication."""
        # POST /api/auth/login — public
        resp = auth_client.post(
            "/api/auth/login",
            json={"email": "nobody@example.com", "password": "whatever123"},
        )
        # Should reach the handler (400 = bad creds, not 401/302 from middleware)
        assert resp.status_code == 400

        # GET /login — public page route
        resp = auth_client.get("/login")
        assert resp.status_code == 200

        # POST /api/auth/accept-invite — public (TASK-100)
        resp = auth_client.post(
            "/api/auth/accept-invite",
            json={"token": "invalid-token", "password": "whatever123"},
        )
        # Should reach handler (400 = invalid token, not 401)
        assert resp.status_code == 400

        # GET /invite/sometoken — public (TASK-100 prefix route)
        resp = auth_client.get("/invite/sometoken")
        # ui router returns 200 (invite page), 302 (redirect), or 404 (token not found)
        assert resp.status_code in (200, 302, 404)


@pytest.mark.integration
class TestSessionLifecycle:
    """Session lifecycle through real middleware."""

    def test_unauthenticated_api_request_returns_401(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """GET /api/auth/me without credentials → 401."""
        make_test_user(auth_db_path)

        fresh_client = TestClient(auth_client.app, raise_server_exceptions=False)
        resp = fresh_client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_unauthenticated_browser_request_redirects_to_login(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """Browser GET without cookie → 302 redirect to /login."""
        make_test_user(auth_db_path)

        fresh_client = TestClient(auth_client.app, raise_server_exceptions=False)
        resp = fresh_client.get(
            "/api/auth/sessions",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]

    def test_expired_session_rejected(self, auth_client: TestClient, auth_db_path: Path) -> None:
        """Manually expired session → 401."""
        make_test_user(auth_db_path, email="expiry@example.com")
        login_resp = login_user(auth_client, "expiry@example.com")
        cookie = login_resp.cookies.get(COOKIE_NAME)
        assert cookie is not None

        # Manually expire all sessions by setting expires_at in the past
        from dango.auth.database import _connect

        conn = _connect(auth_db_path)
        try:
            past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            conn.execute("UPDATE sessions SET expires_at = ?", (past,))
            conn.commit()
        finally:
            conn.close()

        # Session should be rejected
        me_resp = auth_client.get("/api/auth/me", headers=auth_headers(cookie=cookie))
        assert me_resp.status_code in (401, 302)

    def test_concurrent_sessions_independent(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """Two sessions → logout one → other still works."""
        make_test_user(auth_db_path, email="concurrent@example.com")

        # Login session A
        client_a = TestClient(auth_client.app, raise_server_exceptions=False)
        resp_a = client_a.post(
            "/api/auth/login",
            json={"email": "concurrent@example.com", "password": "securepassword123"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp_a.status_code == 200
        cookie_a = resp_a.cookies.get(COOKIE_NAME)
        assert cookie_a is not None

        # Login session B
        client_b = TestClient(auth_client.app, raise_server_exceptions=False)
        resp_b = client_b.post(
            "/api/auth/login",
            json={"email": "concurrent@example.com", "password": "securepassword123"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp_b.status_code == 200
        cookie_b = resp_b.cookies.get(COOKIE_NAME)
        assert cookie_b is not None

        # Both sessions should work
        me_a = client_a.get("/api/auth/me", headers=auth_headers(cookie=cookie_a))
        assert me_a.status_code == 200
        me_b = client_b.get("/api/auth/me", headers=auth_headers(cookie=cookie_b))
        assert me_b.status_code == 200

        # Logout session A
        client_a.post(
            "/api/auth/logout",
            headers=auth_headers(cookie=cookie_a),
        )

        # Session B should still work
        me_b2 = client_b.get("/api/auth/me", headers=auth_headers(cookie=cookie_b))
        assert me_b2.status_code == 200

        # Session A should be rejected
        me_a2 = client_a.get("/api/auth/me", headers=auth_headers(cookie=cookie_a))
        assert me_a2.status_code in (401, 302)


@pytest.mark.integration
class TestRateLimiting:
    """Rate limiting through real middleware."""

    def test_rate_limiting_login_endpoint(self, auth_db_path: Path) -> None:
        """Burst login attempts → 429 after limit exceeded."""
        from dango.web.routes.auth import router as auth_router

        app = FastAPI()
        project_root = auth_db_path.parent.parent
        app.state.project_root = project_root

        # Auth middleware (innermost)
        app.add_middleware(AuthMiddleware, project_root=project_root, idle_timeout_minutes=60)
        # Rate limit with very low threshold (outermost)
        rate_config = RateLimitConfig(
            enabled=True,
            login=RateLimitGroupConfig(requests=3, window_seconds=60),
            api=RateLimitGroupConfig(requests=100, window_seconds=60),
        )
        app.add_middleware(RateLimitMiddleware, config=rate_config)
        app.include_router(auth_router)

        client = TestClient(app, raise_server_exceptions=False)
        make_test_user(auth_db_path, email="ratelimit@example.com")

        # Fire requests (3 allowed, 4th+ should be blocked)
        statuses: list[int] = []
        for _ in range(5):
            resp = client.post(
                "/api/auth/login",
                json={"email": "ratelimit@example.com", "password": "wrongpassword1"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            statuses.append(resp.status_code)

        # At least one should be 429
        assert 429 in statuses, f"Expected 429 in {statuses}"


@pytest.mark.integration
class TestInvitePermissions:
    """TASK-100 invite permission enforcement through middleware."""

    def test_reinvite_requires_admin_permission(
        self, auth_client: TestClient, auth_db_path: Path
    ) -> None:
        """Editor → POST /api/admin/users/{id}/reinvite → 403."""
        # Create target user with invite
        raw_token, token_hash = generate_invite_token()
        target = make_test_user(
            auth_db_path,
            email="target@example.com",
            password_hash=None,
            invite_token_hash=token_hash,
            invite_expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )

        # Login as Editor
        make_test_user(auth_db_path, email="editor@example.com", role=Role.EDITOR)
        login_user(auth_client, "editor@example.com")

        # Try to reinvite — should be 403
        resp = auth_client.post(
            f"/api/admin/users/{target.id}/reinvite",
            headers=auth_headers(auth_client),
        )
        assert resp.status_code == 403
