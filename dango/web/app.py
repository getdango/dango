"""dango/web/app.py

FastAPI application entry point. Creates the app, registers middleware,
mounts static files, includes all route modules, and installs global
exception handlers.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import dango
from dango.config.models import AuthConfig, RateLimitConfig
from dango.exceptions import (
    AccountDeactivatedError,
    AccountLockedError,
    AuthenticationError,
    AuthorizationError,
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
    CSVSchemaMismatchError,
    DangoError,
    DbtLockError,
    DiskSpaceError,
    DuckDBHealthError,
    InfrastructureError,
    IngestionError,
    ProjectNotFoundError,
    SessionExpiredError,
    SyncTimeoutError,
    UserExistsError,
    UserNotFoundError,
    ValidationError,
    WebAPIError,
    is_debug_mode,
)
from dango.logging import get_logger
from dango.web.middleware import AuthMiddleware, RateLimitMiddleware

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown logic."""
    project_root: Path = app.state.project_root
    logger.info("api_starting", project_root=str(project_root))

    # Write auth.yml if missing (migration path for pre-075d projects)
    try:
        from dango.auth.admin import get_auth_config_path, set_auth_enabled

        auth_config_path = get_auth_config_path(project_root)
        if not auth_config_path.exists():
            set_auth_enabled(project_root, enabled=True)
            logger.info("auth_auto_enabled", reason="auth.yml missing")
    except Exception:
        logger.warning("auth_yml_migration_failed", exc_info=True)

    # First-run admin creation (non-interactive)
    try:
        import os

        from dango.auth.admin import ensure_admin, format_credentials_panel, get_auth_db_path
        from dango.cli.utils import console

        db_path = get_auth_db_path(project_root)
        if db_path.exists():
            email = os.environ.get("DANGO_ADMIN_EMAIL", "admin@localhost")
            result = ensure_admin(db_path, email=email)
            if result is not None:
                user, password = result
                console.print()
                console.print(format_credentials_panel(user.email, password))
                console.print()

                # Sync newly created admin to Metabase (if Metabase is running)
                try:
                    from dango.auth.metabase_bridge import (
                        ensure_metabase_synced,
                        get_metabase_url,
                    )

                    mb_url = await get_metabase_url(project_root)
                    if mb_url is not None:
                        await ensure_metabase_synced(db_path, user.id, project_root, mb_url)
                except Exception:
                    logger.debug("metabase_sync_on_admin_create_skipped", exc_info=True)
    except Exception:
        logger.warning("first_run_admin_check_failed", exc_info=True)

    # Cloud security warnings
    try:
        from dango.web.helpers import is_cloud_deployment

        if is_cloud_deployment(project_root):
            auth_cfg = _load_auth_config(project_root)
            if auth_cfg is None:
                auth_cfg = AuthConfig()
            if not auth_cfg.require_2fa:
                logger.warning(
                    "cloud_security_recommendation",
                    setting="require_2fa",
                    recommendation="Enable 2FA for cloud deployments",
                )
            if auth_cfg.idle_timeout_minutes > 60:
                logger.warning(
                    "cloud_security_recommendation",
                    setting="idle_timeout_minutes",
                    current=auth_cfg.idle_timeout_minutes,
                    recommendation="Set idle timeout to 60 minutes or less",
                )
            if auth_cfg.session_max_days > 30:
                logger.warning(
                    "cloud_security_recommendation",
                    setting="session_max_days",
                    current=auth_cfg.session_max_days,
                    recommendation="Set session max to 30 days or less",
                )
    except Exception:
        logger.debug("cloud_security_check_skipped", exc_info=True)

    # Initialize APScheduler for job scheduling
    try:
        import asyncio

        from dango.platform.scheduling import SchedulerService

        scheduler = SchedulerService(project_root)
        scheduler.start(asyncio.get_running_loop())
        app.state.scheduler = scheduler
    except Exception:
        logger.error("scheduler_startup_failed", exc_info=True)
        app.state.scheduler = None

    # Start background sync status watcher (multi-worker support)
    try:
        from dango.platform.sync_process import start_sync_status_watcher

        sync_watcher_task = await start_sync_status_watcher(project_root)
        app.state.sync_watcher_task = sync_watcher_task
    except Exception:
        logger.debug("sync_status_watcher_start_failed", exc_info=True)
        app.state.sync_watcher_task = None

    yield

    # Shutdown
    import asyncio

    sync_task = getattr(app.state, "sync_watcher_task", None)
    if sync_task is not None:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass

    sched = getattr(app.state, "scheduler", None)
    if sched is not None:
        try:
            # Run in thread to avoid blocking the event loop if a job is
            # still executing in the ThreadPoolExecutor.
            await asyncio.to_thread(sched.shutdown, True)
        except Exception:
            logger.error("scheduler_shutdown_failed", exc_info=True)

    logger.info("api_shutting_down")


def create_app(project_root: Path | None = None) -> FastAPI:
    """Create and configure FastAPI application.

    Args:
        project_root: Path to Dango project root (defaults to current directory)

    Returns:
        Configured FastAPI app
    """
    application = FastAPI(
        title="Dango API",
        description="API for managing and monitoring Dango data pipelines",
        version="0.1.0",
        docs_url=None,  # Disable default docs, we'll create custom ones with navbar
        redoc_url=None,  # Disable default redoc
        lifespan=lifespan,
    )

    # Resolve project_root first (needed by middleware)
    if project_root is None:
        project_root = Path.cwd()
    application.state.project_root = project_root

    # Middleware stack (LIFO: last added = outermost in request flow)
    # Request flow: CORS → RateLimit → Auth → Route handlers

    # Auth middleware (innermost — executes after rate limit in request flow)
    auth_config = _load_auth_config(project_root)
    idle_timeout = (
        auth_config.idle_timeout_minutes if auth_config else AuthConfig().idle_timeout_minutes
    )
    application.add_middleware(
        AuthMiddleware, project_root=project_root, idle_timeout_minutes=idle_timeout
    )

    # Rate limiting (middle) — auto-inject trusted_proxies for cloud
    rate_limit_config = _load_rate_limit_config(project_root)
    if rate_limit_config is not None:
        application.add_middleware(RateLimitMiddleware, config=rate_limit_config)

    # CORS (outermost — handles OPTIONS preflight first)
    cors_origins = _get_cors_origins(project_root)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return application


def _load_auth_config(project_root: Path | None) -> AuthConfig | None:
    """Try to load auth config from project.yml. Returns None on failure."""
    try:
        from dango.config.helpers import load_config

        root = project_root or Path.cwd()
        config = load_config(root)
        return config.auth
    except Exception:
        logger.debug("auth_config_not_loaded", reason="no project config found, using defaults")
        return None


def _load_rate_limit_config(project_root: Path | None) -> RateLimitConfig | None:
    """Try to load rate limit config from project.yml. Returns None on failure.

    On cloud deployments, auto-injects ``["127.0.0.1"]`` into
    ``trusted_proxies`` when the user hasn't configured any — Caddy
    connects to uvicorn via localhost.
    """
    try:
        from dango.config.helpers import load_config
        from dango.config.loader import ConfigLoader

        root = project_root or Path.cwd()
        config = load_config(root)
        rl = config.auth.rate_limit

        if not rl.trusted_proxies:
            loader = ConfigLoader(root)
            cloud_cfg = loader.load_cloud_config()
            if cloud_cfg is not None and cloud_cfg.droplet_ip is not None:
                rl = rl.model_copy(update={"trusted_proxies": ["127.0.0.1"]})

        return rl
    except Exception:
        logger.debug(
            "rate_limit_config_not_loaded", reason="no project config found, using defaults"
        )
        return None


def _get_cors_origins(project_root: Path | None) -> list[str]:
    """Determine CORS allowed origins based on deployment mode."""
    try:
        from dango.config.helpers import get_cloud_origin

        root = project_root or Path.cwd()
        origin = get_cloud_origin(root)
        if origin is not None:
            return [origin]
    except Exception:
        pass
    return ["*"]


app = create_app()

# Mount static files directory
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------------------------------------------------------------------------
# Global exception handlers
# ---------------------------------------------------------------------------

# Map exception types to HTTP status codes
_STATUS_MAP: dict[type[DangoError], int] = {
    # Auth errors (subclasses must be present so MRO walk finds them)
    SessionExpiredError: 401,
    AccountLockedError: 423,
    AccountDeactivatedError: 403,
    AuthenticationError: 401,
    AuthorizationError: 403,
    UserNotFoundError: 404,
    UserExistsError: 409,
    # Config errors
    ConfigNotFoundError: 404,
    ProjectNotFoundError: 404,
    ConfigValidationError: 422,
    ConfigError: 500,
    # Ingestion errors
    CSVSchemaMismatchError: 422,
    SyncTimeoutError: 504,
    IngestionError: 500,
    # Infrastructure errors
    DiskSpaceError: 503,
    DuckDBHealthError: 503,
    DbtLockError: 409,
    InfrastructureError: 500,
    # Validation errors
    ValidationError: 400,
    # Web errors
    WebAPIError: 500,
    # Explicit fallback (makes default status code visible in code)
    DangoError: 500,
}


def _is_browser_request(request: Request) -> bool:
    """Return True if the request likely comes from a browser (not API/XHR)."""
    accept = request.headers.get("accept", "")
    xhr = request.headers.get("x-requested-with", "")
    return "text/html" in accept and not xhr and not request.url.path.startswith("/api/")


@app.exception_handler(DangoError)
async def dango_error_handler(request: Request, exc: DangoError) -> Response:
    """Return structured JSON for API requests; HTML error page for browser page requests."""
    # Walk the MRO to find the most specific status code
    status_code = 500
    for cls in type(exc).__mro__:
        if cls in _STATUS_MAP:
            status_code = _STATUS_MAP[cls]
            break

    # Render a friendly HTML error page for browser navigation to page routes
    if status_code in (401, 403) and _is_browser_request(request):
        try:
            from dango.web.routes.ui import _render_template

            titles = {401: "Authentication Required", 403: "Access Denied"}
            return _render_template(
                request,
                "error.html",
                {
                    "status_code": status_code,
                    "error_title": titles.get(status_code, "Error"),
                    "error_message": exc.user_message,
                    "current_page": "",
                    "subtitle": titles.get(status_code, "Error"),
                    "version": dango.__version__,
                },
                status_code=status_code,
            )
        except Exception as e:
            logger.warning("error_page_render_failed", path=request.url.path, error=str(e))
            # Fall through to JSON response

    body: dict = {
        "error_code": exc.error_code,
        "message": exc.user_message,
    }
    if is_debug_mode() and exc.context:
        body["detail"] = exc.context

    return JSONResponse(status_code=status_code, content=body)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
    """Catch-all for unexpected exceptions (return generic 500)."""
    # Delegate HTTPExceptions to FastAPI's built-in handler
    if isinstance(exc, HTTPException):
        return await http_exception_handler(request, exc)
    # Delegate Pydantic request validation errors to FastAPI's built-in handler
    if isinstance(exc, RequestValidationError):
        return await request_validation_exception_handler(request, exc)

    logger.error("unhandled_exception", path=request.url.path, error=str(exc), exc_info=True)

    body: dict = {
        "error_code": "DANGO-G001",
        "message": "An internal error occurred.",
    }
    if is_debug_mode():
        body["detail"] = str(exc)

    return JSONResponse(status_code=500, content=body)


# ---------------------------------------------------------------------------
# Register routers — Dango API routes first, then proxy routes (catch-all last)
# ---------------------------------------------------------------------------
from dango.web.routes.ai import router as ai_router  # noqa: E402
from dango.web.routes.auth import router as auth_router  # noqa: E402
from dango.web.routes.auth_2fa import router as auth_2fa_router  # noqa: E402
from dango.web.routes.catalog import router as catalog_router  # noqa: E402
from dango.web.routes.config import router as config_router  # noqa: E402
from dango.web.routes.dbt import router as dbt_router  # noqa: E402
from dango.web.routes.governance import router as governance_router  # noqa: E402
from dango.web.routes.health import router as health_router  # noqa: E402
from dango.web.routes.initial_sync import router as initial_sync_router  # noqa: E402
from dango.web.routes.logs import router as logs_router  # noqa: E402
from dango.web.routes.metabase_proxy import router as metabase_proxy_router  # noqa: E402
from dango.web.routes.monitoring import router as monitoring_router  # noqa: E402
from dango.web.routes.notebooks import router as notebooks_router  # noqa: E402
from dango.web.routes.oauth_connect import router as oauth_connect_router  # noqa: E402
from dango.web.routes.query import router as query_router  # noqa: E402
from dango.web.routes.schedules import router as schedules_router  # noqa: E402
from dango.web.routes.secrets import router as secrets_router  # noqa: E402
from dango.web.routes.sources import router as sources_router  # noqa: E402
from dango.web.routes.sync import router as sync_router  # noqa: E402
from dango.web.routes.ui import router as ui_router  # noqa: E402
from dango.web.routes.upload import router as upload_router  # noqa: E402
from dango.web.routes.users import router as users_router  # noqa: E402
from dango.web.routes.websocket import router as websocket_router  # noqa: E402

# Dango API routers (order matters — more specific routes first)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(auth_2fa_router)
app.include_router(health_router)
app.include_router(config_router)
app.include_router(sources_router)
app.include_router(sync_router)
app.include_router(initial_sync_router)
app.include_router(logs_router)
app.include_router(upload_router)
app.include_router(secrets_router)
app.include_router(oauth_connect_router)
app.include_router(dbt_router)
app.include_router(schedules_router)
app.include_router(catalog_router)
app.include_router(governance_router)
app.include_router(query_router)
app.include_router(monitoring_router)
app.include_router(ai_router)
app.include_router(notebooks_router)
app.include_router(websocket_router)
app.include_router(ui_router)

# Proxy routers last (catch-all routes like /metabase/{path:path})
app.include_router(metabase_proxy_router)


if __name__ == "__main__":
    import uvicorn

    # Run server for local development
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True, log_level="info")
