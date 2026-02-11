"""dango/web/routes/ui.py

UI page endpoints and API documentation.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import dango

router = APIRouter(tags=["ui"])


def _inject_version(html_content: str) -> str:
    """Replace version placeholder with actual dango version."""
    return html_content.replace("{{DANGO_VERSION}}", dango.__version__)


@router.get("/", response_class=HTMLResponse)
async def root():
    """Serve the dashboard UI."""
    index_file = Path(__file__).parent.parent / "static" / "index.html"

    if index_file.exists():
        return _inject_version(index_file.read_text(encoding="utf-8"))
    else:
        # Fallback if static files not found
        return """
        <html>
            <head><title>Dango - Setup Required</title></head>
            <body>
                <h1>Dango Web UI</h1>
                <p>Static files not found. Please ensure the installation is complete.</p>
                <p>API documentation available at: <a href="/api/docs">/api/docs</a></p>
            </body>
        </html>
        """


@router.get("/health", response_class=HTMLResponse)
async def health_page():
    """Serve the platform health page."""
    health_file = Path(__file__).parent.parent / "static" / "health.html"

    if health_file.exists():
        return _inject_version(health_file.read_text(encoding="utf-8"))
    else:
        return "<html><body><h1>Health page not found</h1></body></html>"


@router.get("/logs", response_class=HTMLResponse)
async def logs_page():
    """Serve the logs page."""
    logs_file = Path(__file__).parent.parent / "static" / "logs.html"

    if logs_file.exists():
        return _inject_version(logs_file.read_text(encoding="utf-8"))
    else:
        return """
        <html>
            <head><title>Logs - Dango</title></head>
            <body>
                <h1>Logs Page Not Found</h1>
                <p><a href="/">Back to Dashboard</a></p>
            </body>
        </html>
        """


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
