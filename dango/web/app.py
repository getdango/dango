"""dango/web/app.py

FastAPI application entry point. Creates the app, registers middleware,
mounts static files, and includes all route modules.
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from dango.web.helpers import get_project_root

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
    logger.info("Dango Web API starting up...")
    logger.info(f"Project root: {get_project_root()}")


@app.on_event("shutdown")
async def shutdown_event():
    """Run on application shutdown."""
    logger.info("Dango Web API shutting down...")


if __name__ == "__main__":
    import uvicorn

    # Run server for local development
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True, log_level="info")
