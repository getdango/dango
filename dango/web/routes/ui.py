"""dango/web/routes/ui.py

UI page endpoints and API documentation.
"""

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound

import dango
from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission

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


def _render_template(request: Request, template_name: str, context: dict) -> HTMLResponse:
    """Render a Jinja2 template with fallback for broken installations."""
    try:
        return templates.TemplateResponse(request, template_name, context=context)
    except TemplateNotFound:
        return HTMLResponse(content=_FALLBACK_HTML)


@router.get("/")
async def root(request: Request) -> HTMLResponse:
    """Serve the dashboard UI."""
    return _render_template(
        request,
        "dashboard.html",
        {
            "version": dango.__version__,
            "current_page": "overview",
            "subtitle": "Data Platform Dashboard",
        },
    )


@router.get("/sources")
async def sources_page(request: Request) -> HTMLResponse:
    """Serve the data sources page."""
    return _render_template(
        request,
        "sources.html",
        {
            "version": dango.__version__,
            "current_page": "sources",
            "subtitle": "Data Sources",
        },
    )


@router.get("/models")
async def models_page(request: Request) -> HTMLResponse:
    """Serve the dbt models page."""
    return _render_template(
        request,
        "models.html",
        {
            "version": dango.__version__,
            "current_page": "models",
            "subtitle": "dbt Models",
        },
    )


@router.get("/health")
async def health_page(request: Request) -> HTMLResponse:
    """Serve the platform health page."""
    return _render_template(
        request,
        "health.html",
        {
            "version": dango.__version__,
            "current_page": "health",
            "subtitle": "Health",
        },
    )


@router.get("/logs")
async def logs_page(request: Request) -> HTMLResponse:
    """Serve the logs page."""
    return _render_template(
        request,
        "logs.html",
        {
            "version": dango.__version__,
            "current_page": "logs",
            "subtitle": "Activity Logs",
        },
    )


@router.get("/catalog")
async def catalog_page(
    request: Request,
    user: User = Depends(require_permission("governance.view")),
) -> HTMLResponse:
    """Serve the data catalog page."""
    log_auth_event(
        AuditEvent.CATALOG_VIEWED,
        user_id=user.id,
        email=user.email,
    )
    return _render_template(
        request,
        "catalog.html",
        {
            "version": dango.__version__,
            "current_page": "catalog",
            "subtitle": "Data Catalog",
        },
    )


@router.get("/insights")
async def insights_page(
    request: Request,
    user: User = Depends(require_permission("governance.view")),
) -> HTMLResponse:
    """Serve the insights page."""
    log_auth_event(
        AuditEvent.INSIGHTS_VIEWED,
        user_id=user.id,
        email=user.email,
    )
    return _render_template(
        request,
        "insights.html",
        {
            "version": dango.__version__,
            "current_page": "insights",
            "subtitle": "Insights",
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
    if getattr(request.state, "user", None):
        from starlette.responses import RedirectResponse

        return RedirectResponse(url="/", status_code=302)  # type: ignore[return-value]
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
        request,
        "login.html",
        {
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
        request,
        "change_password.html",
        {
            "version": dango.__version__,
            "current_page": "setup",
            "subtitle": "Change Password",
        },
    )


@router.get("/invite/{token}")
async def invite_page(token: str, request: Request) -> HTMLResponse:
    """Render the invite acceptance page."""
    from dango.auth.admin import get_auth_db_path
    from dango.auth.database import get_user_by_invite_token_hash
    from dango.auth.security import hash_token

    error: str | None = None
    email: str | None = None

    try:
        project_root: Path = request.app.state.project_root
        db_path = get_auth_db_path(project_root)
        token_hash = hash_token(token)
        user = get_user_by_invite_token_hash(db_path, token_hash)

        invalid_msg = "This invite link is invalid or has expired."
        if user is None:
            error = invalid_msg
        elif user.invite_expires_at is None or user.invite_expires_at <= datetime.now(timezone.utc):
            error = invalid_msg
        else:
            email = user.email
    except Exception:
        error = "Unable to process invite. Please try again later."

    return _render_template(
        request,
        "invite.html",
        {
            "version": dango.__version__,
            "current_page": "invite",
            "subtitle": "Accept Invite",
            "token": token,
            "email": email,
            "invite_error": error,
        },
    )
