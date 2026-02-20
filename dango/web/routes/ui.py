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
async def root(request: Request) -> HTMLResponse:
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
async def health_page(request: Request) -> HTMLResponse:
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
async def logs_page(request: Request) -> HTMLResponse:
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
async def api_info() -> dict[str, str]:
    """API information endpoint."""
    return {"message": "Dango API", "version": "0.1.0", "docs": "/api/docs", "websocket": "/ws"}


@router.get("/api/docs", include_in_schema=False)
async def custom_swagger_ui_html() -> HTMLResponse:
    """Swagger UI (default, no custom navbar)."""
    from fastapi.openapi.docs import get_swagger_ui_html

    return get_swagger_ui_html(openapi_url="/openapi.json", title="Dango API - Documentation")


@router.get("/api/redoc", include_in_schema=False)
async def custom_redoc_html() -> HTMLResponse:
    """ReDoc (default, no custom navbar)."""
    from fastapi.openapi.docs import get_redoc_html

    return get_redoc_html(openapi_url="/openapi.json", title="Dango API - Documentation")


@router.get("/login")
async def login_page(request: Request) -> HTMLResponse:
    """Render the login page."""
    oauth_providers: list[dict[str, str]] = []
    try:
        from dango.auth.oauth_login import get_configured_providers
        from dango.web.routes.auth import _get_oauth_config

        oauth_configs = _get_oauth_config(request)
        providers = get_configured_providers(oauth_configs)
        oauth_providers = [
            {"name": p.name, "display_name": p.display_name, "icon_svg": p.icon_svg}
            for p in providers
        ]
    except Exception:
        pass
    return _render_template(
        "login.html",
        {
            "request": request,
            "version": dango.__version__,
            "current_page": "login",
            "subtitle": "Login",
            "oauth_providers": oauth_providers,
        },
    )


@router.get("/setup")
async def setup_page(request: Request) -> HTMLResponse:
    """Render the change-password page (first-login setup)."""
    return _render_template(
        "change_password.html",
        {
            "request": request,
            "version": dango.__version__,
            "current_page": "setup",
            "subtitle": "Change Password",
        },
    )
