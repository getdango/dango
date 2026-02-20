"""tests/integration/test_metabase_sso.py

Metabase SSO integration tests.  Mocks at the HTTP library level so all
Python code paths (lazy imports, config I/O, DB updates) execute.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.database import get_user_by_id
from dango.auth.models import Role, User
from dango.auth.security import hash_password
from dango.auth.sessions import create_session
from dango.exceptions import DangoError
from dango.migrations.runner import MigrationRunner
from dango.web.middleware.auth import COOKIE_NAME
from dango.web.routes.auth import router as auth_router
from dango.web.routes.users import router as users_router

MB_URL = "http://localhost:3000"
_REQUESTS = "dango.auth.metabase_sync.requests"
_ENCRYPT = "dango.auth.metabase_sync.encrypt_metabase_password"
_HTTPX_CLIENT = "dango.auth.metabase_bridge.httpx.AsyncClient"
_DECRYPT = "dango.auth.metabase_sync.decrypt_metabase_password"

_GROUPS = [
    {"id": 1, "name": "All Users"},
    {"id": 2, "name": "Administrators"},
    {"id": 10, "name": "Dango Editors"},
]


def _make_db(tmp_path: Path) -> Path:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir(exist_ok=True)
    db_path = dango_dir / "auth.db"
    mig_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=mig_dir).apply_pending()
    return db_path


def _make_user(db_path: Path, email: str = "test@example.com", **kw: Any) -> User:
    defaults: dict[str, Any] = {
        "email": email,
        "password_hash": hash_password("securepassword123"),
        "role": Role.EDITOR,
    }
    defaults.update(kw)
    user = User(**defaults)
    db.create_user(db_path, user)
    return user


def _mb_yml(tmp_path: Path) -> Path:
    d = tmp_path / ".dango"
    d.mkdir(exist_ok=True)
    (d / "metabase.yml").write_text(
        yaml.safe_dump(
            {
                "metabase_url": MB_URL,
                "admin": {"email": "admin@dango.local", "password": "dangolocal123"},
                "database": {"id": 1, "name": "Test Analytics"},
            }
        )
    )
    return tmp_path


def _resp(status: int = 200, data: Any = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = data if data is not None else {}
    return r


def _setup_mb_api(mock_req: MagicMock, mb_user_id: int = 42) -> None:
    def _post(url: str, **_kw: Any) -> MagicMock:
        if url.endswith("/api/session"):
            return _resp(200, {"id": "admin-session-token"})
        if url.endswith("/api/user"):
            return _resp(200, {"id": mb_user_id, "email": "test@example.com"})
        if url.endswith("/api/permissions/group"):
            return _resp(200, {"id": 10, "name": "Dango Editors"})
        if url.endswith("/api/permissions/membership"):
            return _resp(200, {})
        return _resp(200)

    def _get(url: str, **_kw: Any) -> MagicMock:
        if "/api/permissions/group" in url and not url.endswith("/group"):
            return _resp(200, {"id": 10, "members": []})
        if url.endswith("/api/permissions/group"):
            return _resp(200, _GROUPS)
        if url.endswith("/api/permissions/graph"):
            return _resp(200, {"groups": {}, "revision": 1})
        if "/api/user?" in url:
            return _resp(200, [])
        if "/api/user/" in url:
            return _resp(200, {"id": mb_user_id, "group_ids": [1]})
        if "/api/database/" in url:
            return _resp(200, {"id": 1, "details": {}})
        return _resp(200)

    mock_req.post.side_effect = _post
    mock_req.get.side_effect = _get
    mock_req.put.side_effect = lambda url, **kw: _resp(200, {"revision": 2})
    mock_req.delete.side_effect = lambda url, **kw: _resp(200)


def _make_app(db_path: Path) -> FastAPI:
    """Create a minimal FastAPI app with auth + users routes."""
    from dango.exceptions import AuthenticationError, AuthorizationError, UserExistsError

    app = FastAPI()
    app.state.project_root = db_path.parent.parent
    _status: dict[type, int] = {
        AuthenticationError: 401,
        AuthorizationError: 403,
        UserExistsError: 409,
    }

    @app.exception_handler(DangoError)
    async def _handler(request: Request, exc: DangoError) -> JSONResponse:
        code = 500
        for cls in type(exc).__mro__:
            if cls in _status:
                code = _status[cls]
                break
        return JSONResponse(status_code=code, content={"message": exc.user_message})

    app.include_router(auth_router)
    app.include_router(users_router)
    return app


def _auth_headers() -> dict[str, str]:
    return {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}


def _mock_httpx_client(
    post_resp: MagicMock | None = None,
    delete_resp: MagicMock | None = None,
    post_effect: Exception | None = None,
) -> AsyncMock:
    c = AsyncMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    if post_effect is not None:
        c.post = AsyncMock(side_effect=post_effect)
    elif post_resp is not None:
        c.post = AsyncMock(return_value=post_resp)
    if delete_resp is not None:
        c.delete = AsyncMock(return_value=delete_resp)
    return c


def _admin_client(
    tmp_path: Path,
    target_kw: dict[str, Any] | None = None,
) -> tuple[TestClient, Path, User, User]:
    db_path = _make_db(tmp_path)
    _mb_yml(tmp_path)
    admin = _make_user(db_path, email="admin@example.com", role=Role.ADMIN)
    tkw: dict[str, Any] = {
        "email": "target@example.com",
        "role": Role.EDITOR,
        "metabase_user_id": 42,
        "metabase_password_enc": "enc",
    }
    if target_kw:
        tkw.update(target_kw)
    target = _make_user(db_path, **tkw)
    app = _make_app(db_path)

    @app.middleware("http")
    async def _inject(request: Any, call_next: Any) -> Any:
        request.state.user = admin
        request.state.auth_method = "session"
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False), db_path, admin, target


@pytest.mark.integration
class TestUserSync:
    """sync_user_to_metabase with real DB state."""

    def test_sync_creates_user_and_stores_id(self, tmp_path: Path) -> None:
        """Creates a Metabase user and persists IDs in auth.db."""
        from dango.auth.metabase_sync import sync_user_to_metabase

        db_path = _make_db(tmp_path)
        _mb_yml(tmp_path)
        user = _make_user(db_path)

        with patch(_REQUESTS) as mock_req, patch(_ENCRYPT, return_value="encrypted_blob"):
            _setup_mb_api(mock_req, mb_user_id=42)
            result = sync_user_to_metabase(db_path, user.id, tmp_path, MB_URL)

        assert result == 42
        updated = get_user_by_id(db_path, user.id)
        assert updated is not None
        assert updated.metabase_user_id == 42
        assert updated.metabase_password_enc == "encrypted_blob"
        # Verify POST /api/user was called
        user_posts = [
            c for c in mock_req.post.call_args_list if c.args and c.args[0].endswith("/api/user")
        ]
        assert len(user_posts) >= 1

    def test_sync_existing_user_applies_role(self, tmp_path: Path) -> None:
        """User with metabase_user_id set skips creation, applies role."""
        from dango.auth.metabase_sync import sync_user_to_metabase

        db_path = _make_db(tmp_path)
        _mb_yml(tmp_path)
        user = _make_user(db_path, metabase_user_id=42)

        with patch(_REQUESTS) as mock_req:
            _setup_mb_api(mock_req, mb_user_id=42)
            result = sync_user_to_metabase(db_path, user.id, tmp_path, MB_URL)

        assert result == 42
        user_posts = [
            c for c in mock_req.post.call_args_list if c.args and c.args[0].endswith("/api/user")
        ]
        assert len(user_posts) == 0


@pytest.mark.integration
class TestRoleMapping:
    """Verify Role → Metabase superuser flag."""

    @pytest.mark.parametrize(
        "role,expect_superuser",
        [(Role.ADMIN, True), (Role.EDITOR, False), (Role.VIEWER, False)],
    )
    def test_role_mapping_superuser_flag(
        self, tmp_path: Path, role: Role, expect_superuser: bool
    ) -> None:
        """Sets is_superuser based on Dango role."""
        from dango.auth.metabase_sync import sync_user_to_metabase

        db_path = _make_db(tmp_path)
        _mb_yml(tmp_path)
        user = _make_user(db_path, role=role)

        with patch(_REQUESTS) as mock_req, patch(_ENCRYPT, return_value="enc"):
            _setup_mb_api(mock_req, mb_user_id=42)
            sync_user_to_metabase(db_path, user.id, tmp_path, MB_URL)

        superuser_puts = [
            c for c in mock_req.put.call_args_list if c.args and "/api/user/" in c.args[0]
        ]
        assert len(superuser_puts) >= 1
        assert superuser_puts[0].kwargs.get("json", {}).get("is_superuser") == expect_superuser


@pytest.mark.integration
class TestAdminActionsSync:
    """Web route → Metabase sync verification."""

    def test_role_change_triggers_metabase_sync(self, tmp_path: Path) -> None:
        client, db_path, _, target = _admin_client(tmp_path)
        with patch(_REQUESTS) as mock_req:
            _setup_mb_api(mock_req, mb_user_id=42)
            resp = client.put(
                f"/api/admin/users/{target.id}/role",
                json={"role": "viewer"},
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        assert mock_req.post.call_count >= 1
        assert get_user_by_id(db_path, target.id) is not None
        assert get_user_by_id(db_path, target.id).role == Role.VIEWER  # type: ignore[union-attr]

    def test_deactivation_triggers_metabase_deactivate(self, tmp_path: Path) -> None:
        client, db_path, _, target = _admin_client(tmp_path)
        with patch(_REQUESTS) as mock_req:
            _setup_mb_api(mock_req, mb_user_id=42)
            resp = client.post(
                f"/api/admin/users/{target.id}/deactivate",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        mb_deletes = [c for c in mock_req.delete.call_args_list if "/api/user/" in str(c)]
        assert len(mb_deletes) >= 1

    def test_deletion_triggers_metabase_delete(self, tmp_path: Path) -> None:
        client, db_path, _, target = _admin_client(tmp_path)
        with patch(_REQUESTS) as mock_req:
            _setup_mb_api(mock_req, mb_user_id=42)
            resp = client.request(
                "DELETE",
                f"/api/admin/users/{target.id}",
                json={"confirm_email": "target@example.com"},
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        mb_deletes = [c for c in mock_req.delete.call_args_list if "/api/user/" in str(c)]
        assert len(mb_deletes) >= 1
        assert get_user_by_id(db_path, target.id) is None


@pytest.mark.integration
class TestSessionBridging:
    """Login/logout → Metabase session lifecycle."""

    def test_login_creates_metabase_session(self, tmp_path: Path) -> None:
        """Login with Metabase credentials bridges a Metabase session."""
        db_path = _make_db(tmp_path)
        _mb_yml(tmp_path)
        _make_user(db_path, metabase_user_id=42, metabase_password_enc="enc")
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)
        mc = _mock_httpx_client(post_resp=_resp(200, {"id": "mb-session-xyz"}))

        with patch(_HTTPX_CLIENT, return_value=mc), patch(_DECRYPT, return_value="pw"):
            resp = client.post(
                "/api/auth/login",
                json={"email": "test@example.com", "password": "securepassword123"},
            )
        assert resp.status_code == 200
        assert COOKIE_NAME in resp.cookies
        assert resp.cookies.get("metabase.SESSION") == "mb-session-xyz"

    def test_logout_destroys_metabase_session(self, tmp_path: Path) -> None:
        """Logout invalidates both Dango + Metabase sessions."""
        db_path = _make_db(tmp_path)
        _mb_yml(tmp_path)
        user = _make_user(db_path)
        raw_token, _ = create_session(db_path, user.id)
        app = _make_app(db_path)

        @app.middleware("http")
        async def _inject(request: Any, call_next: Any) -> Any:
            request.state.user = user
            request.state.auth_method = "session"
            return await call_next(request)

        client = TestClient(app, raise_server_exceptions=False)
        mc = _mock_httpx_client(delete_resp=_resp(204))

        with patch(_HTTPX_CLIENT, return_value=mc):
            resp = client.post(
                "/api/auth/logout",
                headers=_auth_headers(),
                cookies={COOKIE_NAME: raw_token, "metabase.SESSION": "mb-old"},
            )
        assert resp.status_code == 200
        assert resp.cookies.get(COOKIE_NAME, "") == ""
        assert resp.cookies.get("metabase.SESSION", "") == ""
        mc.delete.assert_called_once()

    def test_login_without_metabase_credentials(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        _make_user(db_path)
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)
        resp = client.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": "securepassword123"},
        )
        assert resp.status_code == 200
        assert COOKIE_NAME in resp.cookies
        assert "metabase.SESSION" not in resp.cookies


@pytest.mark.integration
class TestGracefulDegradation:
    """Operations succeed when Metabase is unavailable."""

    def test_login_succeeds_when_metabase_down(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        _mb_yml(tmp_path)
        _make_user(db_path, metabase_user_id=42, metabase_password_enc="enc")
        client = TestClient(_make_app(db_path), raise_server_exceptions=False)
        mc = _mock_httpx_client(post_effect=httpx.ConnectError("refused"))

        with patch(_HTTPX_CLIENT, return_value=mc), patch(_DECRYPT, return_value="pw"):
            resp = client.post(
                "/api/auth/login",
                json={"email": "test@example.com", "password": "securepassword123"},
            )
        assert resp.status_code == 200
        assert COOKIE_NAME in resp.cookies
        assert "metabase.SESSION" not in resp.cookies

    def test_admin_actions_succeed_when_metabase_down(self, tmp_path: Path) -> None:
        client, db_path, _, target = _admin_client(tmp_path)

        with patch(_REQUESTS) as mock_req:
            mock_req.post.side_effect = ConnectionError("refused")
            mock_req.get.side_effect = ConnectionError("refused")
            mock_req.delete.side_effect = ConnectionError("refused")

            resp = client.put(
                f"/api/admin/users/{target.id}/role",
                json={"role": "viewer"},
                headers=_auth_headers(),
            )
            assert resp.status_code == 200
            assert get_user_by_id(db_path, target.id).role == Role.VIEWER  # type: ignore[union-attr]

            resp = client.post(
                f"/api/admin/users/{target.id}/deactivate",
                headers=_auth_headers(),
            )
            assert resp.status_code == 200


def _setup_reconcile_api(
    mock_req: MagicMock,
    user_list: list[dict[str, Any]],
    create_user_status: int = 200,
) -> None:
    """Configure mock routing for ``sync_all_users_to_metabase``."""
    next_id = [100]

    def _get(url: str, **_kw: Any) -> MagicMock:
        if "/api/permissions/group" in url and not url.endswith("/group"):
            return _resp(200, {"id": 10, "members": []})
        if url.endswith("/api/permissions/group"):
            return _resp(200, _GROUPS)
        if url.endswith("/api/permissions/graph"):
            return _resp(200, {"groups": {}, "revision": 1})
        if "/api/user?" in url:
            return _resp(200, user_list)
        if "/api/user/" in url:
            return _resp(200, {"id": 10, "group_ids": [1]})
        if "/api/database/" in url:
            return _resp(200, {"id": 1, "details": {}})
        return _resp(200)

    def _post(url: str, **_kw: Any) -> MagicMock:
        if url.endswith("/api/session"):
            return _resp(200, {"id": "admin-token"})
        if url.endswith("/api/user"):
            if create_user_status != 200:
                return _resp(create_user_status, {"message": "error"})
            uid = next_id[0]
            next_id[0] += 1
            return _resp(200, {"id": uid, "email": "new"})
        if url.endswith("/api/permissions/group"):
            return _resp(200, {"id": 10, "name": "Dango Editors"})
        if url.endswith("/api/permissions/membership"):
            return _resp(200, {})
        return _resp(200)

    mock_req.post.side_effect = _post
    mock_req.get.side_effect = _get
    mock_req.put.side_effect = lambda url, **kw: _resp(200, {"revision": 2})
    mock_req.delete.side_effect = lambda url, **kw: _resp(200)


@pytest.mark.integration
class TestReconciliation:
    """sync_all_users_to_metabase with various DB states."""

    def test_reconciliation_syncs_all_users(self, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import sync_all_users_to_metabase

        db_path = _make_db(tmp_path)
        _mb_yml(tmp_path)
        _make_user(db_path, email="synced@ex.com", role=Role.EDITOR, metabase_user_id=10)
        _make_user(db_path, email="new1@ex.com", role=Role.VIEWER)
        _make_user(db_path, email="new2@ex.com", role=Role.EDITOR)

        with patch(_REQUESTS) as mock_req, patch(_ENCRYPT, return_value="enc"):
            _setup_reconcile_api(
                mock_req,
                user_list=[{"id": 10, "email": "synced@ex.com", "is_active": True}],
            )
            result = sync_all_users_to_metabase(db_path, tmp_path, MB_URL)

        assert result["synced"] == 1
        assert result["created"] == 2
        assert result["errors"] == []

    def test_reconciliation_reports_errors(self, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import sync_all_users_to_metabase

        db_path = _make_db(tmp_path)
        _mb_yml(tmp_path)
        _make_user(db_path, email="fail@example.com", role=Role.VIEWER)

        with patch(_REQUESTS) as mock_req, patch(_ENCRYPT, return_value="enc"):
            _setup_reconcile_api(mock_req, user_list=[], create_user_status=500)
            result = sync_all_users_to_metabase(db_path, tmp_path, MB_URL)

        assert result["created"] == 0
        assert len(result["errors"]) >= 1
