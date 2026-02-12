"""dango/web/routes/ui.py

UI page endpoints and API documentation.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import dango

router = APIRouter(tags=["ui"])

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/")
async def root(request: Request):
    """Serve the dashboard UI."""
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "version": dango.__version__,
            "current_page": "overview",
            "subtitle": "Data Platform Dashboard",
        },
    )


@router.get("/health")
async def health_page(request: Request):
    """Serve the platform health page."""
    return templates.TemplateResponse(
        "health.html",
        {
            "request": request,
            "version": dango.__version__,
            "current_page": "health",
            "subtitle": "Health",
        },
    )


@router.get("/logs")
async def logs_page(request: Request):
    """Serve the logs page."""
    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "version": dango.__version__,
            "current_page": "logs",
            "subtitle": "Activity Logs",
        },
    )


@router.get("/api")
async def api_info():
    """API information endpoint."""
    return {"message": "Dango API", "version": "0.1.0", "docs": "/api/docs", "websocket": "/ws"}


@router.get("/api/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    """Swagger UI (default, no custom navbar)."""
    from fastapi.openapi.docs import get_swagger_ui_html

    return get_swagger_ui_html(openapi_url="/openapi.json", title="Dango API - Documentation")


@router.get("/api/redoc", include_in_schema=False)
async def custom_redoc_html():
    """ReDoc (default, no custom navbar)."""
    from fastapi.openapi.docs import get_redoc_html

    return get_redoc_html(openapi_url="/openapi.json", title="Dango API - Documentation")
