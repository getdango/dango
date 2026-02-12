"""dango/web/routes/ui.py

UI page endpoints and API documentation.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound

import dango

router = APIRouter(tags=["ui"])

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_FALLBACK_HTML = """
<html>
    <head><title>Dango - Setup Required</title></head>
    <body>
        <h1>Dango Web UI</h1>
        <p>Templates not found. Please ensure the installation is complete.</p>
        <p>API documentation available at: <a href="/api/docs">/api/docs</a></p>
    </body>
</html>
"""


def _render_template(template_name: str, context: dict) -> HTMLResponse:
    """Render a Jinja2 template with fallback for broken installations."""
    try:
        return templates.TemplateResponse(template_name, context)
    except TemplateNotFound:
        return HTMLResponse(content=_FALLBACK_HTML)


@router.get("/")
async def root(request: Request):
    """Serve the dashboard UI."""
    return _render_template(
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
    return _render_template(
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
    return _render_template(
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
