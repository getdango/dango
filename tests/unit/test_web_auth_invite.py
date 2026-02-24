"""tests/unit/test_web_auth_invite.py

Tests for invite link functionality: generation, acceptance, reinvite,
invite page rendering, and related model/database operations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.models import Role, User, UserResponse, UserUpdate
from dango.auth.security import generate_invite_token, hash_password, hash_token, verify_password
from dango.exceptions import DangoError
from dango.migrations.runner import MigrationRunner
from dango.web.routes.auth import router as auth_router
from dango.web.routes.ui import router as ui_router
from dango.web.routes.users import router as users_router

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


def _make_user(db_path: Path, email: str = "test@example.com", **kw: Any) -> User:
    """Create and persist a user."""
    defaults: dict[str, Any] = {"email": email, "role": Role.EDITOR}
    defaults.update(kw)
    user = User(**defaults)
    db.create_user(db_path, user)
    return user


def _make_invited_user(
    db_path: Path,
    email: str = "invited@example.com",
    hours_valid: int = 72,
) -> tuple[User, str]:
    """Create a user with a pending invite. Returns (user, raw_token)."""
    raw_token, token_hash = generate_invite_token()
    user = _make_user(
        db_path,
        email=email,
        password_hash=None,
        invite_token_hash=token_hash,
        invite_expires_at=datetime.now(timezone.utc) + timedelta(hours=hours_valid),
    )
    return user, raw_token


def _make_app(db_path: Path) -> FastAPI:
    """Create a minimal FastAPI app with auth, users, and ui routes."""
    from dango.exceptions import (
        AuthenticationError,
        AuthorizationError,
        UserExistsError,
        UserNotFoundError,
    )

    app = FastAPI()
    app.state.project_root = db_path.parent.parent
    status_map: dict[type[DangoError], int] = {
        AuthenticationError: 401,
        AuthorizationError: 403,
        UserNotFoundError: 404,
        UserExistsError: 409,
        DangoError: 500,
    }

    @app.exception_handler(DangoError)
    async def dango_error_handler(request: Request, exc: DangoError) -> JSONResponse:
        code = 500
        for cls in type(exc).__mro__:
            if cls in status_map:
                code = status_map[cls]
                break
        return JSONResponse(
            status_code=code, content={"error_code": exc.error_code, "message": exc.user_message}
        )

    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(ui_router)
    return app


_H = {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}


def _admin_client(tmp_path: Path) -> tuple[TestClient, Path, User]:
    """Set up a test client with an admin user injected via middleware."""
    db_path = _make_db(tmp_path)
    admin = _make_user(
        db_path, "admin@example.com", role=Role.ADMIN, password_hash=hash_password("adminpass123")
    )
    app = _make_app(db_path)

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = admin
        request.state.auth_method = "session"
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False), db_path, admin


# ---------------------------------------------------------------------------
# generate_invite_token()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateInviteToken:
    """Tests for the generate_invite_token() security function."""

    def test_returns_url_safe_token_and_sha256_hash(self) -> None:
        """Token is URL-safe, hash is 64-char hex, and they correspond."""
        raw, hashed = generate_invite_token()
        assert len(raw) >= 40
        assert all(c.isalnum() or c in "-_" for c in raw)
        assert len(hashed) == 64
        assert hash_token(raw) == hashed

    def test_unique_tokens(self) -> None:
        """Each call produces a unique token."""
        tokens = {generate_invite_token()[0] for _ in range(10)}
        assert len(tokens) == 10


# ---------------------------------------------------------------------------
# POST /api/auth/accept-invite
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAcceptInvite:
    """Tests for the accept-invite endpoint."""

    def test_happy_path(self, tmp_path: Path) -> None:
        """Valid token + strong password sets the password and clears invite."""
        db_path = _make_db(tmp_path)
        user, raw_token = _make_invited_user(db_path)
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)

        resp = client.post(
            "/api/auth/accept-invite",
            json={"token": raw_token, "password": "MyNewSecurePass99"},
            headers=_H,
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "Password set successfully"

        updated = db.get_user_by_id(db_path, user.id)
        assert updated is not None
        assert verify_password("MyNewSecurePass99", updated.password_hash)  # type: ignore[arg-type]
        assert updated.invite_token_hash is None
        assert updated.invite_expires_at is None

    def test_expired_token(self, tmp_path: Path) -> None:
        """Expired invite token returns 400."""
        db_path = _make_db(tmp_path)
        _, raw_token = _make_invited_user(db_path, hours_valid=-1)
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)

        resp = client.post(
            "/api/auth/accept-invite",
            json={"token": raw_token, "password": "MyNewSecurePass99"},
            headers=_H,
        )
        assert resp.status_code == 400
        assert "invalid or has expired" in resp.json()["message"]

    def test_invalid_token(self, tmp_path: Path) -> None:
        """Unknown token returns 400 with same message as expired."""
        db_path = _make_db(tmp_path)
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)

        resp = client.post(
            "/api/auth/accept-invite",
            json={"token": "bogus-token", "password": "MyNewSecurePass99"},
            headers=_H,
        )
        assert resp.status_code == 400
        assert "invalid or has expired" in resp.json()["message"]

    def test_already_used_token(self, tmp_path: Path) -> None:
        """Token that was already accepted returns 400."""
        db_path = _make_db(tmp_path)
        _, raw_token = _make_invited_user(db_path)
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)

        client.post(
            "/api/auth/accept-invite",
            json={"token": raw_token, "password": "MyNewSecurePass99"},
            headers=_H,
        )
        resp = client.post(
            "/api/auth/accept-invite",
            json={"token": raw_token, "password": "AnotherPass123"},
            headers=_H,
        )
        assert resp.status_code == 400

    def test_weak_password(self, tmp_path: Path) -> None:
        """Weak password returns 400 with issues."""
        db_path = _make_db(tmp_path)
        _, raw_token = _make_invited_user(db_path)
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)

        resp = client.post(
            "/api/auth/accept-invite", json={"token": raw_token, "password": "short"}, headers=_H
        )
        assert resp.status_code == 400
        assert len(resp.json()["issues"]) > 0

    def test_inactive_user_token_rejected(self, tmp_path: Path) -> None:
        """Inactive user's invite token returns 400."""
        db_path = _make_db(tmp_path)
        raw_token, token_hash = generate_invite_token()
        _make_user(
            db_path,
            "inactive@example.com",
            is_active=False,
            password_hash=None,
            invite_token_hash=token_hash,
            invite_expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)

        resp = client.post(
            "/api/auth/accept-invite",
            json={"token": raw_token, "password": "MyNewSecurePass99"},
            headers=_H,
        )
        assert resp.status_code == 400

    def test_invalid_body(self, tmp_path: Path) -> None:
        """Malformed request returns 400."""
        db_path = _make_db(tmp_path)
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)

        resp = client.post("/api/auth/accept-invite", content=b"not json", headers=_H)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/admin/users (invite flow)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateUserInvite:
    """Tests for admin user creation with invite flow."""

    def test_default_creates_invite(self, tmp_path: Path) -> None:
        """Default user creation returns invite_url, not temp_password."""
        client, db_path, _ = _admin_client(tmp_path)
        resp = client.post(
            "/api/admin/users", json={"email": "new@example.com", "role": "viewer"}, headers=_H
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "invite_url" in data
        assert "/invite/" in data["invite_url"]
        assert "temp_password" not in data

        user = db.get_user_by_email(db_path, "new@example.com")
        assert user is not None
        assert user.password_hash is None
        assert user.invite_token_hash is not None

    def test_generate_password_flag(self, tmp_path: Path) -> None:
        """generate_password=true uses temp password flow."""
        client, db_path, _ = _admin_client(tmp_path)
        resp = client.post(
            "/api/admin/users",
            json={"email": "new@example.com", "generate_password": True},
            headers=_H,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "temp_password" in data
        assert "invite_url" not in data

    def test_invited_user_has_pending_invite(self, tmp_path: Path) -> None:
        """UserResponse has has_pending_invite=True for invited user."""
        client, _, _ = _admin_client(tmp_path)
        resp = client.post("/api/admin/users", json={"email": "new@example.com"}, headers=_H)
        assert resp.json()["user"]["has_pending_invite"] is True

    def test_duplicate_email_invite(self, tmp_path: Path) -> None:
        """Creating invite for existing email returns 409."""
        client, db_path, _ = _admin_client(tmp_path)
        _make_user(db_path, email="existing@example.com")
        resp = client.post("/api/admin/users", json={"email": "existing@example.com"}, headers=_H)
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /api/admin/users/{user_id}/reinvite
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReinvite:
    """Tests for the reinvite endpoint."""

    def test_happy_path(self, tmp_path: Path) -> None:
        """Reinviting generates a new token and invalidates the old one."""
        client, db_path, _ = _admin_client(tmp_path)
        user, _ = _make_invited_user(db_path)
        old_hash = db.get_user_by_id(db_path, user.id)
        assert old_hash is not None

        resp = client.post(f"/api/admin/users/{user.id}/reinvite", headers=_H)
        assert resp.status_code == 200
        assert "/invite/" in resp.json()["invite_url"]

        updated = db.get_user_by_id(db_path, user.id)
        assert updated is not None
        assert updated.invite_token_hash != old_hash.invite_token_hash

    def test_expired_invite_can_be_resent(self, tmp_path: Path) -> None:
        """Can reinvite a user whose invite expired."""
        client, db_path, _ = _admin_client(tmp_path)
        user, _ = _make_invited_user(db_path, hours_valid=-1)

        resp = client.post(f"/api/admin/users/{user.id}/reinvite", headers=_H)
        assert resp.status_code == 200

    def test_user_not_found(self, tmp_path: Path) -> None:
        """Returns 404 for non-existent user."""
        client, _, _ = _admin_client(tmp_path)
        resp = client.post("/api/admin/users/nonexistent/reinvite", headers=_H)
        assert resp.status_code == 404

    def test_user_with_password(self, tmp_path: Path) -> None:
        """Cannot reinvite user who already set a password."""
        client, db_path, _ = _admin_client(tmp_path)
        user = _make_user(db_path, "active@example.com", password_hash=hash_password("pass123"))
        resp = client.post(f"/api/admin/users/{user.id}/reinvite", headers=_H)
        assert resp.status_code == 400
        assert "already set a password" in resp.json()["message"]

    def test_deactivated_user(self, tmp_path: Path) -> None:
        """Cannot reinvite a deactivated user."""
        client, db_path, _ = _admin_client(tmp_path)
        user, _ = _make_invited_user(db_path, email="deactivated@example.com")
        db.update_user(db_path, user.id, UserUpdate(is_active=False))
        resp = client.post(f"/api/admin/users/{user.id}/reinvite", headers=_H)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /invite/{token}
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInvitePage:
    """Tests for the invite page rendering."""

    def test_valid_invite(self, tmp_path: Path) -> None:
        """Valid invite renders form with email."""
        db_path = _make_db(tmp_path)
        user, raw_token = _make_invited_user(db_path)
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)

        resp = client.get(f"/invite/{raw_token}")
        assert resp.status_code == 200
        assert user.email in resp.text
        assert "invalid or has expired" not in resp.text.lower()

    def test_expired_invite(self, tmp_path: Path) -> None:
        """Expired invite renders error."""
        db_path = _make_db(tmp_path)
        _, raw_token = _make_invited_user(db_path, hours_valid=-1)
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)

        resp = client.get(f"/invite/{raw_token}")
        assert resp.status_code == 200
        assert "expired" in resp.text.lower()

    def test_invalid_token(self, tmp_path: Path) -> None:
        """Invalid token renders error."""
        db_path = _make_db(tmp_path)
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)

        resp = client.get("/invite/bogus-token")
        assert resp.status_code == 200
        assert "invalid or has expired" in resp.text.lower()


# ---------------------------------------------------------------------------
# UserResponse computed field + database lookup
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInviteModelAndDB:
    """Tests for invite-related model fields and DB lookup."""

    def test_has_pending_invite_true(self) -> None:
        """has_pending_invite is True for future expiry."""
        user = User(
            email="t@e.com", invite_expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        assert UserResponse.model_validate(user).has_pending_invite is True

    def test_has_pending_invite_false_expired(self) -> None:
        """has_pending_invite is False for past expiry."""
        user = User(
            email="t@e.com", invite_expires_at=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        assert UserResponse.model_validate(user).has_pending_invite is False

    def test_has_pending_invite_false_none(self) -> None:
        """has_pending_invite is False when no invite exists."""
        assert UserResponse.model_validate(User(email="t@e.com")).has_pending_invite is False

    def test_db_lookup_by_token_hash(self, tmp_path: Path) -> None:
        """get_user_by_invite_token_hash returns matching user."""
        db_path = _make_db(tmp_path)
        user, raw_token = _make_invited_user(db_path)
        found = db.get_user_by_invite_token_hash(db_path, hash_token(raw_token))
        assert found is not None
        assert found.id == user.id

    def test_db_lookup_not_found(self, tmp_path: Path) -> None:
        """get_user_by_invite_token_hash returns None for unknown hash."""
        db_path = _make_db(tmp_path)
        assert db.get_user_by_invite_token_hash(db_path, "a" * 64) is None

    def test_db_lookup_inactive_excluded(self, tmp_path: Path) -> None:
        """Inactive users are excluded from invite token lookup."""
        db_path = _make_db(tmp_path)
        raw_token, token_hash = generate_invite_token()
        _make_user(
            db_path,
            "inactive@e.com",
            is_active=False,
            password_hash=None,
            invite_token_hash=token_hash,
            invite_expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        assert db.get_user_by_invite_token_hash(db_path, token_hash) is None
