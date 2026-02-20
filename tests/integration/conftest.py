"""tests/integration/conftest.py

Integration test fixtures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth import database as auth_db
from dango.auth.models import Role, User
from dango.auth.security import hash_password
from dango.exceptions import (
    AccountDeactivatedError,
    AccountLockedError,
    AuthenticationError,
    AuthorizationError,
    DangoError,
    SessionExpiredError,
    UserExistsError,
    UserNotFoundError,
)
from dango.migrations.runner import MigrationRunner
from dango.web.middleware.auth import COOKIE_NAME, AuthMiddleware
from dango.web.routes.auth import router as auth_router
from dango.web.routes.auth_2fa import router as auth_2fa_router
from dango.web.routes.ui import router as ui_router
from dango.web.routes.users import router as users_router


@pytest.fixture
def test_duckdb(tmp_path: Path) -> Path:
    """Create a temporary DuckDB database with standard Dango schemas.

    Returns the path to the database file.
    """
    db_path = tmp_path / "test_warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
        conn.execute("CREATE SCHEMA IF NOT EXISTS staging")
        conn.execute("CREATE SCHEMA IF NOT EXISTS intermediate")
        conn.execute("CREATE SCHEMA IF NOT EXISTS marts")
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Auth integration fixtures
# ---------------------------------------------------------------------------

# DangoError → HTTP status mapping (auth-subset of app.py's _STATUS_MAP)
_STATUS_MAP: dict[type[DangoError], int] = {
    SessionExpiredError: 401,
    AccountLockedError: 423,
    AccountDeactivatedError: 403,
    AuthenticationError: 401,
    AuthorizationError: 403,
    UserNotFoundError: 404,
    UserExistsError: 409,
    DangoError: 500,
}


@pytest.fixture
def auth_db_path(tmp_path: Path) -> Path:
    """Create a fresh auth database with auth enabled."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    db_path = dango_dir / "auth.db"
    migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    runner = MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=migrations_dir)
    runner.apply_pending()
    # Enable auth
    auth_yml = dango_dir / "auth.yml"
    auth_yml.write_text("enabled: true\n")
    return db_path


@pytest.fixture
def auth_app(auth_db_path: Path) -> FastAPI:
    """Create FastAPI app with auth middleware and all auth routers."""
    app = FastAPI()
    project_root = auth_db_path.parent.parent
    app.state.project_root = project_root
    app.add_middleware(AuthMiddleware, project_root=project_root, idle_timeout_minutes=60)
    # Register routers
    app.include_router(auth_router)
    app.include_router(auth_2fa_router)
    app.include_router(users_router)
    app.include_router(ui_router)

    # DangoError exception handler (mirrors app.py)
    @app.exception_handler(DangoError)
    async def dango_error_handler(request: Request, exc: DangoError) -> JSONResponse:
        status_code = 500
        for cls in type(exc).__mro__:
            if cls in _STATUS_MAP:
                status_code = _STATUS_MAP[cls]
                break
        return JSONResponse(
            status_code=status_code,
            content={"error_code": exc.error_code, "message": exc.user_message},
        )

    return app


@pytest.fixture
def auth_client(auth_app: FastAPI) -> TestClient:
    """TestClient with auth middleware."""
    return TestClient(auth_app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Auth test helpers
# ---------------------------------------------------------------------------


def make_test_user(
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
    auth_db.create_user(db_path, user)
    return user


def login_user(
    client: TestClient,
    email: str = "test@example.com",
    password: str = "securepassword123",
) -> Any:
    """Login a user and return the response.

    The session cookie is extracted and stored on ``client._session_cookie``
    for subsequent ``auth_headers()`` calls.
    """
    resp = client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    # Store cookie on the client for auth_headers() to pick up.
    # TestClient's automatic cookie forwarding doesn't reliably work with
    # ASGI middleware that parses raw headers, so we pass Cookie explicitly.
    cookie = resp.cookies.get(COOKIE_NAME)
    if cookie:
        client._session_cookie = cookie  # type: ignore[attr-defined]
    return resp


def auth_headers(
    client: TestClient | None = None,
    csrf: bool = True,
    cookie: str | None = None,
) -> dict[str, str]:
    """Return headers for authenticated requests.

    Automatically includes the session cookie from a prior ``login_user()`` call
    when *client* is provided. Pass *cookie* to override explicitly.
    """
    headers: dict[str, str] = {}
    if csrf:
        headers["X-Requested-With"] = "XMLHttpRequest"
    resolved_cookie = cookie
    if resolved_cookie is None and client is not None:
        resolved_cookie = getattr(client, "_session_cookie", None)
    if resolved_cookie is not None:
        headers["Cookie"] = f"{COOKIE_NAME}={resolved_cookie}"
    return headers
