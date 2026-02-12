"""dango/web/app.py

FastAPI application entry point. Creates the app, registers middleware,
mounts static files, includes all route modules, and installs global
exception handlers.
"""

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

from dango.exceptions import (
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
    CSVSchemaMismatchError,
    DangoError,
    DbtLockError,
    DiskSpaceError,
    DuckDBHealthError,
    IngestionError,
    ProjectNotFoundError,
    SyncTimeoutError,
    ValidationError,
    WebAPIError,
    is_debug_mode,
)
from dango.logging import get_logger
from dango.web.helpers import get_project_root

logger = get_logger(__name__)


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
    )

    # Add CORS middleware for development
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # In production, restrict to specific origins
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store project root in app state
    if project_root is None:
        project_root = Path.cwd()
    application.state.project_root = project_root

    return application


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
    # Validation errors
    ValidationError: 400,
    # Web errors
    WebAPIError: 500,
}


@app.exception_handler(DangoError)
async def dango_error_handler(request: Request, exc: DangoError) -> JSONResponse:
    """Return structured JSON for all DangoError subclasses."""
    # Walk the MRO to find the most specific status code
    status_code = 500
    for cls in type(exc).__mro__:
        if cls in _STATUS_MAP:
            status_code = _STATUS_MAP[cls]
            break

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
from dango.web.routes.config import router as config_router  # noqa: E402
from dango.web.routes.dbt import router as dbt_router  # noqa: E402
from dango.web.routes.health import router as health_router  # noqa: E402
from dango.web.routes.logs import router as logs_router  # noqa: E402
from dango.web.routes.metabase_proxy import router as metabase_proxy_router  # noqa: E402
from dango.web.routes.sources import router as sources_router  # noqa: E402
from dango.web.routes.sync import router as sync_router  # noqa: E402
from dango.web.routes.ui import router as ui_router  # noqa: E402
from dango.web.routes.upload import router as upload_router  # noqa: E402
from dango.web.routes.websocket import router as websocket_router  # noqa: E402

# Dango API routers (order matters — more specific routes first)
app.include_router(health_router)
app.include_router(config_router)
app.include_router(sources_router)
app.include_router(sync_router)
app.include_router(logs_router)
app.include_router(upload_router)
app.include_router(dbt_router)
app.include_router(websocket_router)
app.include_router(ui_router)

# Proxy routers last (catch-all routes like /metabase/{path:path})
app.include_router(metabase_proxy_router)


# Application startup/shutdown events
@app.on_event("startup")
async def startup_event():
    """Run on application startup."""
    logger.info("api_starting", project_root=str(get_project_root()))


@app.on_event("shutdown")
async def shutdown_event():
    """Run on application shutdown."""
    logger.info("api_shutting_down")


if __name__ == "__main__":
    import uvicorn

    # Run server for local development
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True, log_level="info")
