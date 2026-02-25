"""tests/integration/conftest.py

Integration test fixtures.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
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


# ---------------------------------------------------------------------------
# Cloud integration fixtures (TEST-005, TEST-006, TEST-007)
# ---------------------------------------------------------------------------

_DEPLOY_ADMIN_EMAIL = "test-admin@dango-test.dev"
_DEPLOY_ADMIN_PASSWORD = os.environ.get("DANGO_ADMIN_PASSWORD", "TestPassword123!")


@pytest.fixture(scope="session")
def require_cloud_env() -> str:
    """Skip entire session if DIGITALOCEAN_TOKEN is not set.

    Returns the token for convenience.
    """
    token = os.environ.get("DIGITALOCEAN_TOKEN")
    if not token:
        pytest.skip("DIGITALOCEAN_TOKEN not set — skipping cloud tests")
    return token


@pytest.fixture(scope="session")
def require_spaces_env(require_cloud_env: str) -> tuple[str, str]:
    """Additionally require SPACES_ACCESS_KEY and SPACES_SECRET_KEY.

    Returns (access_key, secret_key) tuple.
    """
    access_key = os.environ.get("SPACES_ACCESS_KEY")
    secret_key = os.environ.get("SPACES_SECRET_KEY")
    if not access_key or not secret_key:
        pytest.skip("SPACES_ACCESS_KEY / SPACES_SECRET_KEY not set — skipping Spaces tests")
    return access_key, secret_key


@pytest.fixture(scope="session")
def do_client(require_cloud_env: str) -> Any:
    """Session-scoped DigitalOceanClient."""
    from dango.platform.cloud.digitalocean import DigitalOceanClient

    return DigitalOceanClient(token=require_cloud_env)


@pytest.fixture
def unique_test_name() -> str:
    """Generate 'dango-test-{uuid[:8]}' for unique resource naming."""
    return f"dango-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def deployed_server(
    require_cloud_env: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[dict[str, Any], None, None]:
    """Deploy a full server, yield deployment info, destroy on teardown.

    Yields a dict with keys:
        - cloud_cfg: CloudConfig
        - project_root: Path
        - ssh: connected SSHManager (as root)
        - client: DigitalOceanClient
        - droplet_ip: str
    """
    from dango.cli.commands.deploy_provision import run_provisioning
    from dango.cli.commands.deploy_wizard import WizardConfig
    from dango.config.loader import ConfigLoader
    from dango.platform.cloud.digitalocean import DigitalOceanClient
    from dango.platform.cloud.ssh import SSHManager

    project_root = tmp_path_factory.mktemp("cloud-deploy")

    # Create minimal project structure
    dango_dir = project_root / ".dango"
    dango_dir.mkdir()
    (dango_dir / "project.yml").write_text(
        "project:\n  name: dango-cloud-test\n  warehouse: data/warehouse.duckdb\n"
    )
    (dango_dir / "sources.yml").write_text("sources: []\n")
    dbt_dir = project_root / "dbt"
    dbt_dir.mkdir()
    (dbt_dir / "dbt_project.yml").write_text(
        "name: dango_cloud_test\nversion: '1.0'\nprofile: dango\nmodel-paths: ['models']\n"
    )
    (dbt_dir / "models").mkdir()
    (dbt_dir / "macros").mkdir()

    config = WizardConfig(
        region="nyc1",
        size_slug="s-1vcpu-1gb",
        size_tier=None,
        domain=None,
        admin_email=_DEPLOY_ADMIN_EMAIL,
        admin_password=_DEPLOY_ADMIN_PASSWORD,
        skip_oauth=True,
        enable_backups=False,
        skip_initial_sync=True,
        monthly_cost=6,
    )

    client = DigitalOceanClient(token=require_cloud_env)
    result = None
    ssh = None

    try:
        result = run_provisioning(project_root, config)

        # Load the saved cloud config
        loader = ConfigLoader(project_root)
        cloud_cfg = loader.load_cloud_config()
        assert cloud_cfg is not None, "Cloud config not saved after provisioning"

        # Connect SSH as root for test use
        key_path = project_root / ".dango" / "cloud_key"
        ssh = SSHManager(key_path=key_path)
        ssh.connect(result.droplet_ip, username="root")

        yield {
            "cloud_cfg": cloud_cfg,
            "project_root": project_root,
            "ssh": ssh,
            "client": client,
            "droplet_ip": result.droplet_ip,
        }

    except SystemExit as exc:
        pytest.fail(f"Provisioning failed with SystemExit({exc.code})")

    finally:
        # Teardown: disconnect SSH, delete DO resources
        if ssh is not None:
            ssh.disconnect()
        if result is not None:
            _cleanup_deployment(client, result.droplet_id, result.firewall_id, result.ssh_key_id)


def _cleanup_deployment(
    client: Any,
    droplet_id: int,
    firewall_id: str,
    ssh_key_id: int,
) -> None:
    """Best-effort cleanup of DO resources after test."""
    for _label, fn in [
        ("firewall", lambda: client.delete_firewall(firewall_id)),
        ("droplet", lambda: client.delete_droplet(droplet_id)),
        ("ssh_key", lambda: client.delete_ssh_key(ssh_key_id)),
    ]:
        try:
            fn()
        except Exception:
            pass  # best-effort — manual cleanup by name prefix
