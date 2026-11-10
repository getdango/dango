"""tests/unit/test_g4f2_enrollment_session.py

With auth.require_2fa, a user who has not enrolled TOTP gets an enrollment-only partial session
(not a full session) from password and OAuth login; the full session comes after TOTP is verified.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pyotp
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.models import Role, User
from dango.auth.oauth_login import OAuthUserInfo
from dango.auth.permissions import require_permission
from dango.auth.security import hash_password, hash_token
from dango.config.models import AuthConfig, OAuthProviderConfig
from dango.exceptions import AuthorizationError
from dango.migrations.runner import MigrationRunner
from dango.web.middleware.auth import COOKIE_NAME, AuthMiddleware
from dango.web.routes.auth import router as auth_router
from dango.web.routes.auth_2fa import router as auth_2fa_router
from dango.web.routes.users import router as users_router

_PASSWORD = "securepassword123"
_MIGRATIONS = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
_AJAX = {"X-Requested-With": "XMLHttpRequest"}
_REQUIRE_2FA = AuthConfig(enabled=True, require_2fa=True)
_GOOGLE = {"google": OAuthProviderConfig(client_id="g-id", client_secret="g-secret")}


def _env(tmp_path: Path) -> tuple[TestClient, Path]:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    db_path = dango_dir / "auth.db"
    MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=_MIGRATIONS).apply_pending()
    (dango_dir / "auth.yml").write_text("enabled: true\n")
    app = FastAPI()
    app.state.project_root = tmp_path
    app.add_middleware(AuthMiddleware, project_root=tmp_path, idle_timeout_minutes=60)
    app.include_router(auth_router)
    app.include_router(auth_2fa_router)
    app.include_router(users_router)

    @app.exception_handler(AuthorizationError)
    async def _forbidden(request: Request, exc: AuthorizationError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"message": "forbidden"})

    return TestClient(app, raise_server_exceptions=False), db_path


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    with (
        patch("dango.web.routes.auth._get_auth_config", return_value=_REQUIRE_2FA),
        patch("dango.web.routes.auth_2fa._get_auth_config", return_value=_REQUIRE_2FA),
    ):
        yield _env(tmp_path)


def _user(db_path: Path, **overrides: Any) -> User:
    fields: dict[str, Any] = {
        "email": "new@example.com",
        "password_hash": hash_password(_PASSWORD),
        "role": Role.ADMIN,
    }
    fields.update(overrides)
    user = User(**fields)
    db.create_user(db_path, user)
    return user


def _cookie(token: str) -> dict[str, str]:
    return {**_AJAX, "Cookie": f"{COOKIE_NAME}={token}"}


def _session_of(db_path: Path, token: str) -> Any:
    return db.get_session_by_token(db_path, hash_token(token))


def _login(client: TestClient) -> Any:
    with patch(
        "dango.auth.metabase_bridge.bridge_metabase_login",
        new_callable=AsyncMock,
        return_value="mb-token",
    ):
        return client.post(
            "/api/auth/login", json={"email": "new@example.com", "password": _PASSWORD}
        )


def _enrolling_cookie(client: TestClient, db_path: Path) -> str:
    _user(db_path)
    token = _login(client).cookies.get(COOKIE_NAME)
    assert token
    return str(token)


@pytest.mark.unit
class TestPasswordLoginEnrollmentState:
    def test_login_issues_partial_session_and_no_full_access(
        self, env: tuple[TestClient, Path]
    ) -> None:
        client, db_path = env
        _user(db_path)
        with patch(
            "dango.auth.metabase_bridge.bridge_metabase_login",
            new_callable=AsyncMock,
            return_value="mb-token",
        ) as bridge:
            resp = client.post(
                "/api/auth/login", json={"email": "new@example.com", "password": _PASSWORD}
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["requires_2fa_setup"] is True
        assert "user" not in data
        session = _session_of(db_path, resp.cookies.get(COOKIE_NAME))
        assert session is not None and session.is_partial is True
        assert "metabase.SESSION" not in resp.cookies
        bridge.assert_not_called()

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "/api/auth/sessions", None),
            ("GET", "/api/auth/api-keys", None),
            ("POST", "/api/auth/api-keys", {"name": "k"}),
            ("GET", "/api/admin/users", None),
        ],
    )
    def test_privileged_routes_refused_while_enrolling(
        self, env: tuple[TestClient, Path], method: str, path: str, body: Any
    ) -> None:
        client, db_path = env
        token = _enrolling_cookie(client, db_path)
        resp = client.request(method, path, json=body, headers=_cookie(token))
        assert resp.status_code in (401, 403)
        owner = db.get_user_by_email(db_path, "new@example.com")
        assert owner is not None
        assert db.list_user_api_keys(db_path, owner.id) == []

    def test_enrollment_endpoints_reachable_and_verification_issues_full_session(
        self, env: tuple[TestClient, Path]
    ) -> None:
        client, db_path = env
        token = _enrolling_cookie(client, db_path)
        setup = client.post(
            "/api/auth/2fa/setup", json={"password": _PASSWORD}, headers=_cookie(token)
        )
        assert setup.status_code == 200
        code = pyotp.TOTP(setup.json()["secret"]).now()
        with patch(
            "dango.auth.metabase_bridge.bridge_metabase_login",
            new_callable=AsyncMock,
            return_value="mb-token",
        ) as bridge:
            verify = client.post(
                "/api/auth/2fa/verify-setup", json={"code": code}, headers=_cookie(token)
            )
        assert verify.status_code == 200
        full_token = verify.cookies.get(COOKIE_NAME)
        assert full_token and full_token != token
        full = _session_of(db_path, full_token)
        assert full is not None and full.is_partial is False
        old = _session_of(db_path, token)
        assert old is None or old.is_active is False
        assert verify.cookies.get("metabase.SESSION") == "mb-token"
        bridge.assert_called_once()
        assert client.get("/api/auth/api-keys", headers=_cookie(full_token)).status_code == 200

    def test_oauth_only_user_can_enroll_without_a_password(
        self, env: tuple[TestClient, Path]
    ) -> None:
        client, db_path = env
        _user(db_path, password_hash=None, oauth_provider="google", oauth_id="g-1")
        info = OAuthUserInfo(
            provider="google", provider_id="g-1", email="new@example.com", name="N"
        )
        provider = MagicMock()
        provider.exchange_code = AsyncMock(return_value=info)
        with (
            patch("dango.web.routes.auth._get_oauth_config", return_value=_GOOGLE),
            patch("dango.web.routes.auth.get_provider", return_value=provider),
        ):
            resp = client.get(
                "/api/auth/oauth/google/callback?code=c&state=s",
                headers={"Cookie": "dango_oauth_state=s"},
                follow_redirects=False,
            )
        token = resp.cookies.get(COOKIE_NAME)
        assert token
        setup = client.post("/api/auth/2fa/setup", json={"password": ""}, headers=_cookie(token))
        assert setup.status_code == 200

    def test_logout_works_from_enrollment_session(self, env: tuple[TestClient, Path]) -> None:
        client, db_path = env
        token = _enrolling_cookie(client, db_path)
        assert client.post("/api/auth/logout", headers=_cookie(token)).status_code == 200
        session = _session_of(db_path, token)
        assert session is None or session.is_active is False

    def test_password_change_keeps_the_enrollment_restriction(
        self, env: tuple[TestClient, Path]
    ) -> None:
        client, db_path = env
        token = _enrolling_cookie(client, db_path)
        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": _PASSWORD, "new_password": "anotherpassword456"},
            headers=_cookie(token),
        )
        assert resp.status_code == 200
        new_token = resp.cookies.get(COOKIE_NAME)
        session = _session_of(db_path, new_token)
        assert session is not None and session.is_partial is True
        assert client.get("/api/auth/api-keys", headers=_cookie(new_token)).status_code in (
            401,
            403,
        )

    def test_verify_state_partial_session_cannot_use_enrollment_endpoints(
        self, env: tuple[TestClient, Path]
    ) -> None:
        """A partial session of an already-enrolled user only completes the TOTP check."""
        from dango.auth.totp import enable_totp, setup_totp

        client, db_path = env
        user = _user(db_path)
        setup_totp(db_path, user.id, pyotp.random_base32(), ["AAAA-BBBB"])
        enable_totp(db_path, user.id)
        token = _login(client).cookies.get(COOKIE_NAME)
        resp = client.post(
            "/api/auth/2fa/setup", json={"password": _PASSWORD}, headers=_cookie(str(token))
        )
        assert resp.status_code in (401, 403)

    def test_login_without_require_2fa_is_unchanged(self, tmp_path: Path) -> None:
        client, db_path = _env(tmp_path)
        _user(db_path)
        with patch("dango.web.routes.auth._get_auth_config", return_value=AuthConfig(enabled=True)):
            resp = _login(client)
        assert "user" in resp.json()
        session = _session_of(db_path, resp.cookies.get(COOKIE_NAME))
        assert session is not None and session.is_partial is False


@pytest.mark.unit
class TestOAuthEnrollmentState:
    def test_oauth_login_without_totp_gets_partial_session(
        self, env: tuple[TestClient, Path]
    ) -> None:
        client, db_path = env
        _user(db_path, oauth_provider="google", oauth_id="g-2")
        info = OAuthUserInfo(
            provider="google", provider_id="g-2", email="new@example.com", name="N"
        )
        provider = MagicMock()
        provider.exchange_code = AsyncMock(return_value=info)
        with (
            patch("dango.web.routes.auth._get_oauth_config", return_value=_GOOGLE),
            patch("dango.web.routes.auth.get_provider", return_value=provider),
            patch(
                "dango.auth.metabase_bridge.bridge_metabase_login",
                new_callable=AsyncMock,
                return_value="mb-token",
            ) as bridge,
        ):
            resp = client.get(
                "/api/auth/oauth/google/callback?code=c&state=s",
                headers={"Cookie": "dango_oauth_state=s"},
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert "requires_2fa_setup=true" in resp.headers["location"]
        session = _session_of(db_path, resp.cookies.get(COOKIE_NAME))
        assert session is not None and session.is_partial is True
        assert "metabase.SESSION" not in resp.cookies
        bridge.assert_not_called()
        key_resp = client.post(
            "/api/auth/api-keys",
            json={"name": "k"},
            headers=_cookie(str(resp.cookies.get(COOKIE_NAME))),
        )
        assert key_resp.status_code in (401, 403)


@pytest.mark.unit
class TestPermissionDependencyRefusesPartialSessions:
    def test_require_permission_rejects_enrollment_session(self) -> None:
        import asyncio

        request = MagicMock()
        request.state.user = User(email="a@example.com", role=Role.ADMIN)
        request.state.session_partial = True
        dependency = require_permission("source.view")
        with pytest.raises(AuthorizationError):
            asyncio.run(dependency(request))
