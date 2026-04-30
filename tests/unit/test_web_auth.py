"""tests/unit/test_web_auth.py

Tests for login/logout/me/change-password endpoints in dango/web/routes/auth.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.models import Role, User
from dango.auth.security import hash_password, hash_token, verify_password
from dango.auth.sessions import DEFAULT_SESSION_MAX_DAYS, create_session
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
    email: str = "test@example.com",
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


def _make_app(db_path: Path) -> FastAPI:
    """Create a minimal FastAPI app with auth routes for testing."""
    app = FastAPI()
    # project_root is the parent of .dango/ which contains auth.db
    app.state.project_root = db_path.parent.parent
    app.include_router(router)
    return app


def _make_client(app: FastAPI) -> TestClient:
    """Create a test client."""
    return TestClient(app, raise_server_exceptions=False)


def _auth_headers() -> dict[str, str]:
    """Standard headers for authenticated requests."""
    return {"X-Requested-With": "XMLHttpRequest"}


def _setup_auth_client(
    tmp_path: Path,
    **user_overrides: Any,
) -> tuple[TestClient, Path, User]:
    """Set up a test client with a logged-in user. Returns (client, db_path, user)."""
    db_path = _make_db(tmp_path)
    user = _make_user(db_path, **user_overrides)
    app = _make_app(db_path)
    client = _make_client(app)

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    return client, db_path, user


# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLogin:
    """Tests for POST /api/auth/login."""

    def test_login_success(self, tmp_path: Path) -> None:
        """Successful login returns user info and sets cookie."""
        db_path = _make_db(tmp_path)
        _make_user(db_path)
        client = _make_client(_make_app(db_path))

        resp = client.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": "securepassword123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["email"] == "test@example.com"
        assert data["must_change_password"] is False
        assert COOKIE_NAME in resp.cookies

    def test_login_wrong_password(self, tmp_path: Path) -> None:
        """Wrong password returns 400 with generic error."""
        db_path = _make_db(tmp_path)
        _make_user(db_path)
        client = _make_client(_make_app(db_path))

        resp = client.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": "wrongpassword"},
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "Invalid email or password"
        assert COOKIE_NAME not in resp.cookies

    def test_login_unknown_email(self, tmp_path: Path) -> None:
        """Unknown email returns 400 with same generic error (no enumeration)."""
        db_path = _make_db(tmp_path)
        _make_user(db_path)
        client = _make_client(_make_app(db_path))

        resp = client.post(
            "/api/auth/login",
            json={"email": "nobody@example.com", "password": "password123"},
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "Invalid email or password"

    def test_login_deactivated_account(self, tmp_path: Path) -> None:
        """Deactivated account returns same generic error."""
        db_path = _make_db(tmp_path)
        _make_user(db_path, is_active=False)
        client = _make_client(_make_app(db_path))

        resp = client.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": "securepassword123"},
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "Invalid email or password"

    def test_login_locked_account(self, tmp_path: Path) -> None:
        """Locked account returns 423 with remaining seconds."""
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        locked_until = datetime.now(timezone.utc) + timedelta(minutes=10)
        db.update_user(
            db_path,
            user.id,
            db.UserUpdate(failed_login_attempts=5, locked_until=locked_until),
        )
        client = _make_client(_make_app(db_path))

        resp = client.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": "securepassword123"},
        )
        assert resp.status_code == 423
        data = resp.json()
        assert "remaining_seconds" in data
        assert data["remaining_seconds"] > 0

    def test_login_must_change_password(self, tmp_path: Path) -> None:
        """Login with must_change_password flag returns it in response."""
        db_path = _make_db(tmp_path)
        _make_user(db_path, must_change_password=True)
        client = _make_client(_make_app(db_path))

        resp = client.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": "securepassword123"},
        )
        assert resp.status_code == 200
        assert resp.json()["must_change_password"] is True

    def test_login_malformed_body(self, tmp_path: Path) -> None:
        """Malformed or missing fields returns 400, not 500."""
        db_path = _make_db(tmp_path)
        client = _make_client(_make_app(db_path))

        resp = client.post("/api/auth/login", content=b"not json")
        assert resp.status_code == 400

        resp = client.post("/api/auth/login", json={"email": "test@example.com"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Logout tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLogout:
    """Tests for POST /api/auth/logout."""

    def test_logout_success(self, tmp_path: Path) -> None:
        """Logout clears cookies and invalidates session."""
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        app = _make_app(db_path)
        raw_token, _session = create_session(db_path, user.id)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = user
            request.state.auth_method = "session"
            return await call_next(request)

        client = _make_client(app)
        client.cookies.set(COOKIE_NAME, raw_token)

        resp = client.post("/api/auth/logout", headers=_auth_headers())
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        retrieved = db.get_session_by_token(db_path, hash_token(raw_token))
        assert retrieved is not None
        assert retrieved.is_active is False

    def test_logout_not_authenticated(self, tmp_path: Path) -> None:
        """Logout without auth returns 401."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = None
            request.state.auth_method = None
            return await call_next(request)

        client = _make_client(app)
        resp = client.post("/api/auth/logout", headers=_auth_headers())
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Me endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMe:
    """Tests for GET /api/auth/me."""

    def test_me_authenticated(self, tmp_path: Path) -> None:
        """Returns user info when authenticated."""
        client, _db_path, user = _setup_auth_client(tmp_path)
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == user.email
        assert data["role"] == user.role.value

    def test_me_auth_disabled(self, tmp_path: Path) -> None:
        """Returns auth_enabled: false when no user is set."""
        db_path = _make_db(tmp_path)
        app = _make_app(db_path)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = None
            request.state.auth_method = None
            return await call_next(request)

        client = _make_client(app)
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        assert resp.json()["auth_enabled"] is False


# ---------------------------------------------------------------------------
# Change password tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChangePassword:
    """Tests for POST /api/auth/change-password."""

    def test_change_password_success(self, tmp_path: Path) -> None:
        """Successful password change invalidates old sessions."""
        client, _db_path, _user = _setup_auth_client(tmp_path)

        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "securepassword123", "new_password": "newpassword456"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert COOKIE_NAME in resp.cookies

    def test_change_password_wrong_current(self, tmp_path: Path) -> None:
        """Wrong current password returns 400."""
        client, _db_path, _user = _setup_auth_client(tmp_path)

        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "wrongpassword", "new_password": "newpassword456"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 400
        assert "incorrect" in resp.json()["message"].lower()

    def test_change_password_weak_new(self, tmp_path: Path) -> None:
        """Weak new password returns 400 with issues."""
        client, _db_path, _user = _setup_auth_client(tmp_path)

        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "securepassword123", "new_password": "short"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 400
        assert "weak" in resp.json()["message"].lower()

    def test_change_password_same_password_rejected(self, tmp_path: Path) -> None:
        """Cannot reuse the current password."""
        client, _db_path, _user = _setup_auth_client(tmp_path)

        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "securepassword123", "new_password": "securepassword123"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 400
        assert "different" in resp.json()["message"].lower()

    def test_change_password_updates_hash(self, tmp_path: Path) -> None:
        """Password change persists the new hash and invalidates old sessions."""
        client, db_path, user = _setup_auth_client(tmp_path)

        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "securepassword123", "new_password": "newpassword456"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

        updated = db.get_user_by_id(db_path, user.id)
        assert updated is not None
        assert updated.password_hash is not None
        assert verify_password("newpassword456", updated.password_hash)
        assert not verify_password("securepassword123", updated.password_hash)

    def test_change_password_clears_must_change(self, tmp_path: Path) -> None:
        """Password change clears must_change_password flag."""
        client, db_path, user = _setup_auth_client(tmp_path, must_change_password=True)

        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "securepassword123", "new_password": "newpassword456"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

        updated = db.get_user_by_id(db_path, user.id)
        assert updated is not None
        assert updated.must_change_password is False


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
class TestCookieMaxAge:
    """Verify session cookies include Max-Age matching configured expiry."""

    def test_login_cookie_has_max_age(self, tmp_path: Path) -> None:
        """Successful login sets Max-Age = session_max_days * 86400."""
        db_path = _make_db(tmp_path)
        _make_user(db_path)
        client = _make_client(_make_app(db_path))

        resp = client.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": "securepassword123"},
        )
        assert resp.status_code == 200
        max_age = _get_cookie_max_age(resp, COOKIE_NAME)
        assert max_age == DEFAULT_SESSION_MAX_DAYS * 86400

    def test_2fa_partial_cookie_has_short_max_age(self, tmp_path: Path) -> None:
        """2FA partial session sets Max-Age = 300 (5 minutes)."""
        db_path = _make_db(tmp_path)
        _make_user(db_path, totp_enabled=True, totp_secret="JBSWY3DPEHPK3PXP")
        client = _make_client(_make_app(db_path))

        resp = client.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": "securepassword123"},
        )
        assert resp.status_code == 200
        assert resp.json()["requires_2fa"] is True
        max_age = _get_cookie_max_age(resp, COOKIE_NAME)
        assert max_age == 300

    def test_change_password_cookie_has_max_age(self, tmp_path: Path) -> None:
        """Password change sets Max-Age on the new session cookie."""
        client, _db_path, _user = _setup_auth_client(tmp_path)

        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "securepassword123", "new_password": "newpassword456"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        max_age = _get_cookie_max_age(resp, COOKIE_NAME)
        assert max_age == DEFAULT_SESSION_MAX_DAYS * 86400
