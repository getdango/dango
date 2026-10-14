"""tests/factories/auth_app_factories.py

Real-file auth project + FastAPI app harness for AuthMiddleware tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth import database as auth_db
from dango.auth import sessions
from dango.auth.models import Role, User
from dango.exceptions import AuthenticationError, AuthorizationError, DangoError
from dango.migrations.runner import MigrationRunner
from dango.web.middleware.auth import COOKIE_NAME, AuthMiddleware

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"

INVALID_YAML = "enabled: [true\n  oops: : :\n"
"""Text that ``yaml.safe_load`` rejects (unterminated flow sequence)."""

_STATUS_MAP: dict[type[DangoError], int] = {
    AuthenticationError: 401,
    AuthorizationError: 403,
}

probe_router = APIRouter()


@probe_router.get("/api/probe")
async def _probe(request: Request) -> dict[str, Any]:
    """Route with no route-level permission: reports what the middleware set."""
    user = getattr(request.state, "user", None)
    return {
        "user": user.email if user is not None else None,
        "auth_method": getattr(request.state, "auth_method", None),
        "auth_disabled": getattr(request.state, "auth_disabled", None),
    }


class AuthProject:
    """A Dango project directory with real ``auth.yml`` / ``auth.db`` files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / ".dango").mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.root / ".dango" / "auth.db"

    @property
    def toggle_path(self) -> Path:
        return self.root / ".dango" / "auth.yml"

    def write_toggle(self, text: str) -> None:
        self.toggle_path.write_text(text)

    def create_db(self) -> Path:
        runner = MigrationRunner(
            db_path=self.db_path, db_name="auth", migrations_dir=_MIGRATIONS_DIR
        )
        runner.apply_pending()
        return self.db_path

    def add_user(self, email: str = "viewer@example.com", role: Role = Role.VIEWER) -> User:
        user = User(email=email, role=role, password_hash="unused-hash")
        auth_db.create_user(self.db_path, user)
        return user

    def session_cookie(self, user: User) -> str:
        token, _session = sessions.create_session(self.db_path, user.id)
        return f"{COOKIE_NAME}={token}"

    def api_key(self, user: User) -> str:
        raw_key, _key = sessions.create_api_key(self.db_path, user.id, "test-key")
        return raw_key


def build_client(
    project_root: Path,
    *routers: APIRouter,
    base_url: str = "http://testserver",
) -> TestClient:
    """Build a FastAPI app with the real AuthMiddleware in front of *routers*."""
    app = FastAPI()
    app.state.project_root = project_root
    app.add_middleware(AuthMiddleware, project_root=project_root, idle_timeout_minutes=60)
    app.include_router(probe_router)
    for router in routers:
        app.include_router(router)

    @app.exception_handler(DangoError)
    async def _dango_error_handler(request: Request, exc: DangoError) -> JSONResponse:
        status_code = 500
        for cls in type(exc).__mro__:
            if cls in _STATUS_MAP:
                status_code = _STATUS_MAP[cls]
                break
        return JSONResponse(status_code=status_code, content={"error_code": exc.error_code})

    return TestClient(app, base_url=base_url, raise_server_exceptions=False)


def find_auth_middleware(client: TestClient) -> AuthMiddleware:
    """Return the AuthMiddleware instance (the stack is built on first request)."""
    app: Any = client.app
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    current = app.middleware_stack
    while current is not None:
        if isinstance(current, AuthMiddleware):
            return current
        current = getattr(current, "app", None)
    raise AssertionError("AuthMiddleware not found in middleware stack")
