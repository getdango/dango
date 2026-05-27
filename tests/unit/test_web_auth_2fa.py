"""tests/unit/test_web_auth_2fa.py

Tests for 2FA endpoints in dango/web/routes/auth_2fa.py and the
2FA login flow integration with dango/web/routes/auth.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.models import Role, User, UserUpdate
from dango.auth.security import hash_password
from dango.auth.sessions import DEFAULT_SESSION_MAX_DAYS
from dango.auth.totp import (
    enable_totp,
    generate_totp_secret,
    hash_and_store_codes,
    setup_totp,
)
from dango.migrations.runner import MigrationRunner
from dango.web.middleware.auth import COOKIE_NAME
from dango.web.routes.auth import router as auth_router
from dango.web.routes.auth_2fa import _2fa_attempt_counts
from dango.web.routes.auth_2fa import router as auth_2fa_router

_TEST_PASSWORD = "securepassword123"
_MIGRATIONS = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"


def _make_db(tmp_path: Path) -> Path:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    db_path = dango_dir / "auth.db"
    MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=_MIGRATIONS).apply_pending()
    return db_path


def _make_user(db_path: Path, **overrides: Any) -> User:
    defaults: dict[str, Any] = {
        "email": "test@example.com",
        "password_hash": hash_password(_TEST_PASSWORD),
        "role": Role.EDITOR,
    }
    defaults.update(overrides)
    user = User(**defaults)
    db.create_user(db_path, user)
    return user


def _make_app(db_path: Path) -> FastAPI:
    app = FastAPI()
    app.state.project_root = db_path.parent.parent
    app.include_router(auth_router)
    app.include_router(auth_2fa_router)
    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _hdrs() -> dict[str, str]:
    return {"X-Requested-With": "XMLHttpRequest"}


def _authed(tmp_path: Path, **kw: Any) -> tuple[TestClient, Path, User]:
    """Client with a logged-in user via middleware mock."""
    db_path = _make_db(tmp_path)
    user = _make_user(db_path, **kw)
    app = _make_app(db_path)
    tc = _client(app)

    @app.middleware("http")
    async def _set(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    return tc, db_path, user


def _enable_2fa(db_path: Path, user_id: str) -> str:
    """Enable 2FA for a user, returning the secret."""
    secret = generate_totp_secret()
    setup_totp(db_path, user_id, secret, ["AAAA-BBBB"])
    enable_totp(db_path, user_id)
    return secret


@pytest.mark.unit
class TestTwoFASetup:
    """POST /api/auth/2fa/setup."""

    def test_success(self, tmp_path: Path) -> None:
        tc, db_path, user = _authed(tmp_path)
        resp = tc.post("/api/auth/2fa/setup", json={"password": _TEST_PASSWORD}, headers=_hdrs())
        assert resp.status_code == 200
        data = resp.json()
        assert data["provisioning_uri"].startswith("otpauth://totp/")
        assert len(data["recovery_codes"]) == 8
        u = db.get_user_by_id(db_path, user.id)
        assert u is not None
        assert u.totp_secret == data["secret"]
        assert u.totp_enabled is False

    def test_wrong_password(self, tmp_path: Path) -> None:
        tc, _, _ = _authed(tmp_path)
        resp = tc.post("/api/auth/2fa/setup", json={"password": "wrong"}, headers=_hdrs())
        assert resp.status_code == 400
        assert "Invalid password" in resp.json()["message"]

    def test_already_enabled_rejects(self, tmp_path: Path) -> None:
        tc, db_path, user = _authed(tmp_path)
        _enable_2fa(db_path, user.id)
        resp = tc.post("/api/auth/2fa/setup", json={"password": _TEST_PASSWORD}, headers=_hdrs())
        assert resp.status_code == 400
        assert "already enabled" in resp.json()["message"]

    def test_unauthenticated(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        _make_user(db_path)
        tc = _client(_make_app(db_path))
        resp = tc.post("/api/auth/2fa/setup", json={"password": _TEST_PASSWORD}, headers=_hdrs())
        assert resp.status_code == 401


@pytest.mark.unit
class TestTwoFAVerifySetup:
    """POST /api/auth/2fa/verify-setup."""

    def test_success(self, tmp_path: Path) -> None:
        tc, db_path, user = _authed(tmp_path)
        resp = tc.post("/api/auth/2fa/setup", json={"password": _TEST_PASSWORD}, headers=_hdrs())
        secret = resp.json()["secret"]
        code = pyotp.TOTP(secret).now()
        resp = tc.post("/api/auth/2fa/verify-setup", json={"code": code}, headers=_hdrs())
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        u = db.get_user_by_id(db_path, user.id)
        assert u is not None and u.totp_enabled is True

    def test_invalid_code(self, tmp_path: Path) -> None:
        tc, _, _ = _authed(tmp_path)
        tc.post("/api/auth/2fa/setup", json={"password": _TEST_PASSWORD}, headers=_hdrs())
        resp = tc.post("/api/auth/2fa/verify-setup", json={"code": "000000"}, headers=_hdrs())
        assert resp.status_code == 400

    def test_no_pending(self, tmp_path: Path) -> None:
        tc, _, _ = _authed(tmp_path)
        resp = tc.post("/api/auth/2fa/verify-setup", json={"code": "123456"}, headers=_hdrs())
        assert resp.status_code == 400
        assert "No pending" in resp.json()["message"]

    def test_already_enabled(self, tmp_path: Path) -> None:
        tc, db_path, user = _authed(tmp_path)
        _enable_2fa(db_path, user.id)
        resp = tc.post("/api/auth/2fa/verify-setup", json={"code": "123456"}, headers=_hdrs())
        assert resp.status_code == 400
        assert "already enabled" in resp.json()["message"]


@pytest.mark.unit
class TestTwoFAVerify:
    """POST /api/auth/2fa/verify (partial session → full session)."""

    def _partial_login(self, tmp_path: Path) -> tuple[TestClient, Path, User, str, str]:
        """Create user with 2FA, login to get partial session cookie."""
        db_path = _make_db(tmp_path)
        secret = generate_totp_secret()
        user = _make_user(db_path, totp_secret=secret, totp_enabled=True)
        hashed = hash_and_store_codes(["AAAA-BBBB", "CCCC-DDDD"])
        db.update_user(db_path, user.id, UserUpdate(recovery_codes=hashed))
        app = _make_app(db_path)
        tc = _client(app)
        resp = tc.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": _TEST_PASSWORD},
        )
        assert resp.status_code == 200 and resp.json()["requires_2fa"] is True
        cookie = resp.cookies.get(COOKIE_NAME)
        assert cookie is not None
        return tc, db_path, user, secret, cookie

    def test_totp_success(self, tmp_path: Path) -> None:
        tc, _, user, secret, cookie = self._partial_login(tmp_path)
        code = pyotp.TOTP(secret).now()
        resp = tc.post(
            "/api/auth/2fa/verify",
            json={"code": code, "is_recovery": False},
            cookies={COOKIE_NAME: cookie},
            headers=_hdrs(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["email"] == "test@example.com"
        assert resp.cookies.get(COOKIE_NAME) is not None

    def test_recovery_code(self, tmp_path: Path) -> None:
        tc, _, _, _, cookie = self._partial_login(tmp_path)
        resp = tc.post(
            "/api/auth/2fa/verify",
            json={"code": "AAAA-BBBB", "is_recovery": True},
            cookies={COOKIE_NAME: cookie},
            headers=_hdrs(),
        )
        assert resp.status_code == 200
        assert "user" in resp.json()

    def test_invalid_code(self, tmp_path: Path) -> None:
        tc, _, _, _, cookie = self._partial_login(tmp_path)
        resp = tc.post(
            "/api/auth/2fa/verify",
            json={"code": "000000", "is_recovery": False},
            cookies={COOKIE_NAME: cookie},
            headers=_hdrs(),
        )
        assert resp.status_code == 400

    def test_no_cookie(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        _make_user(db_path)
        tc = _client(_make_app(db_path))
        resp = tc.post("/api/auth/2fa/verify", json={"code": "123456"}, headers=_hdrs())
        assert resp.status_code == 401

    def test_invalid_session(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        _make_user(db_path)
        tc = _client(_make_app(db_path))
        resp = tc.post(
            "/api/auth/2fa/verify",
            json={"code": "123456"},
            cookies={COOKIE_NAME: "bogus"},
            headers=_hdrs(),
        )
        assert resp.status_code == 401

    def test_expired_partial_session(self, tmp_path: Path) -> None:
        """A partial session past its 5-min window should be rejected."""
        import sqlite3

        tc, db_path, user, secret, cookie = self._partial_login(tmp_path)
        # Expire the partial session by backdating expires_at
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE sessions SET expires_at = ? WHERE is_partial = 1", (past,))
        conn.commit()
        conn.close()
        code = pyotp.TOTP(secret).now()
        resp = tc.post(
            "/api/auth/2fa/verify",
            json={"code": code, "is_recovery": False},
            cookies={COOKIE_NAME: cookie},
            headers=_hdrs(),
        )
        assert resp.status_code == 401


@pytest.mark.unit
class TestTwoFABruteForce:
    """2FA brute-force protection — lockout after 5 failed attempts."""

    def _partial_login(self, tmp_path: Path) -> tuple[TestClient, Path, User, str, str]:
        """Create user with 2FA, login to get partial session cookie."""
        db_path = _make_db(tmp_path)
        secret = generate_totp_secret()
        user = _make_user(db_path, totp_secret=secret, totp_enabled=True)
        hashed = hash_and_store_codes(["AAAA-BBBB", "CCCC-DDDD"])
        db.update_user(db_path, user.id, UserUpdate(recovery_codes=hashed))
        app = _make_app(db_path)
        tc = _client(app)
        resp = tc.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": _TEST_PASSWORD},
        )
        assert resp.status_code == 200 and resp.json()["requires_2fa"] is True
        cookie = resp.cookies.get(COOKIE_NAME)
        assert cookie is not None
        # Clear any stale counters
        _2fa_attempt_counts.clear()
        return tc, db_path, user, secret, cookie

    def test_lockout_after_5_failures(self, tmp_path: Path) -> None:
        tc, _, _, _, cookie = self._partial_login(tmp_path)
        # 4 failures → still 400 (invalid code)
        for _ in range(4):
            resp = tc.post(
                "/api/auth/2fa/verify",
                json={"code": "000000", "is_recovery": False},
                cookies={COOKIE_NAME: cookie},
                headers=_hdrs(),
            )
            assert resp.status_code == 400

        # 5th failure → 401 lockout
        resp = tc.post(
            "/api/auth/2fa/verify",
            json={"code": "000000", "is_recovery": False},
            cookies={COOKIE_NAME: cookie},
            headers=_hdrs(),
        )
        assert resp.status_code == 401
        assert "Too many failed attempts" in resp.json()["message"]

    def test_subsequent_request_after_lockout_also_401(self, tmp_path: Path) -> None:
        tc, _, _, secret, cookie = self._partial_login(tmp_path)
        # Exhaust attempts
        for _ in range(5):
            tc.post(
                "/api/auth/2fa/verify",
                json={"code": "000000", "is_recovery": False},
                cookies={COOKIE_NAME: cookie},
                headers=_hdrs(),
            )
        # Even a valid code should fail — session invalidated
        code = pyotp.TOTP(secret).now()
        resp = tc.post(
            "/api/auth/2fa/verify",
            json={"code": code, "is_recovery": False},
            cookies={COOKIE_NAME: cookie},
            headers=_hdrs(),
        )
        assert resp.status_code == 401


@pytest.mark.unit
class TestTwoFADisable:
    """POST /api/auth/2fa/disable."""

    def test_success(self, tmp_path: Path) -> None:
        tc, db_path, user = _authed(tmp_path)
        _enable_2fa(db_path, user.id)
        resp = tc.post("/api/auth/2fa/disable", json={"password": _TEST_PASSWORD}, headers=_hdrs())
        assert resp.status_code == 200
        u = db.get_user_by_id(db_path, user.id)
        assert u is not None and u.totp_enabled is False and u.totp_secret is None

    def test_wrong_password(self, tmp_path: Path) -> None:
        tc, db_path, user = _authed(tmp_path)
        _enable_2fa(db_path, user.id)
        resp = tc.post("/api/auth/2fa/disable", json={"password": "wrong"}, headers=_hdrs())
        assert resp.status_code == 400

    def test_not_enabled(self, tmp_path: Path) -> None:
        tc, _, _ = _authed(tmp_path)
        resp = tc.post("/api/auth/2fa/disable", json={"password": _TEST_PASSWORD}, headers=_hdrs())
        assert resp.status_code == 400
        assert "not enabled" in resp.json()["message"]


@pytest.mark.unit
class TestTwoFARegenerate:
    """POST /api/auth/2fa/regenerate-recovery."""

    def test_success(self, tmp_path: Path) -> None:
        tc, db_path, user = _authed(tmp_path)
        secret = _enable_2fa(db_path, user.id)
        code = pyotp.TOTP(secret).now()
        resp = tc.post(
            "/api/auth/2fa/regenerate-recovery",
            json={"password": _TEST_PASSWORD, "code": code},
            headers=_hdrs(),
        )
        assert resp.status_code == 200
        assert len(resp.json()["recovery_codes"]) == 8

    def test_wrong_password(self, tmp_path: Path) -> None:
        tc, db_path, user = _authed(tmp_path)
        secret = _enable_2fa(db_path, user.id)
        resp = tc.post(
            "/api/auth/2fa/regenerate-recovery",
            json={"password": "wrong", "code": pyotp.TOTP(secret).now()},
            headers=_hdrs(),
        )
        assert resp.status_code == 400

    def test_wrong_totp(self, tmp_path: Path) -> None:
        tc, db_path, user = _authed(tmp_path)
        _enable_2fa(db_path, user.id)
        resp = tc.post(
            "/api/auth/2fa/regenerate-recovery",
            json={"password": _TEST_PASSWORD, "code": "000000"},
            headers=_hdrs(),
        )
        assert resp.status_code == 400


@pytest.mark.unit
class TestLoginWith2FA:
    """Login endpoint 2FA integration."""

    def test_totp_enabled_returns_requires_2fa(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        _make_user(db_path, totp_secret=generate_totp_secret(), totp_enabled=True)
        tc = _client(_make_app(db_path))
        resp = tc.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": _TEST_PASSWORD},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["requires_2fa"] is True
        assert COOKIE_NAME in resp.cookies
        assert "user" not in data

    def test_normal_login_no_2fa(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        _make_user(db_path)
        tc = _client(_make_app(db_path))
        resp = tc.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": _TEST_PASSWORD},
        )
        data = resp.json()
        assert "user" in data
        assert data["requires_2fa_setup"] is False

    def test_require_2fa_flag(self, tmp_path: Path) -> None:
        """require_2fa=True config → requires_2fa_setup in response."""
        db_path = _make_db(tmp_path)
        _make_user(db_path)
        # Config lives in .dango/project.yml (write BEFORE creating client)
        dango_dir = db_path.parent  # tmp_path / ".dango"
        (dango_dir / "project.yml").write_text(
            "project:\n  name: test\n  created_by: tester\n  purpose: testing\n"
            "auth:\n  require_2fa: true\n"
        )
        tc = _client(_make_app(db_path))
        resp = tc.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": _TEST_PASSWORD},
        )
        data = resp.json()
        assert data["requires_2fa_setup"] is True
        assert "user" in data


# ---------------------------------------------------------------------------
# Cookie max_age tests
# ---------------------------------------------------------------------------


def _get_cookie_max_age(resp: Any, cookie_name: str) -> int | None:
    """Extract Max-Age from a Set-Cookie header for a given cookie name."""
    for header in resp.headers.get_list("set-cookie"):
        if header.startswith(f"{cookie_name}="):
            for part in header.split(";"):
                part = part.strip()
                if part.lower().startswith("max-age="):
                    return int(part.split("=", 1)[1])
    return None


@pytest.mark.unit
class TestTwoFACookieMaxAge:
    """Verify 2FA verify endpoint sets Max-Age on session cookie."""

    def test_2fa_verify_cookie_has_max_age(self, tmp_path: Path) -> None:
        """Successful 2FA verification sets Max-Age = session_max_days * 86400."""
        db_path = _make_db(tmp_path)
        secret = generate_totp_secret()
        user = _make_user(db_path, totp_secret=secret, totp_enabled=True)
        hashed = hash_and_store_codes(["AAAA-BBBB", "CCCC-DDDD"])
        db.update_user(db_path, user.id, UserUpdate(recovery_codes=hashed))
        app = _make_app(db_path)
        tc = _client(app)

        # Get partial session
        resp = tc.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": _TEST_PASSWORD},
        )
        assert resp.status_code == 200
        cookie = resp.cookies.get(COOKIE_NAME)
        assert cookie is not None

        # Verify 2FA
        code = pyotp.TOTP(secret).now()
        resp = tc.post(
            "/api/auth/2fa/verify",
            json={"code": code, "is_recovery": False},
            cookies={COOKIE_NAME: cookie},
            headers=_hdrs(),
        )
        assert resp.status_code == 200
        max_age = _get_cookie_max_age(resp, COOKIE_NAME)
        assert max_age == DEFAULT_SESSION_MAX_DAYS * 86400
