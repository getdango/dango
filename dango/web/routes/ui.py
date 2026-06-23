"""dango/web/routes/ui.py

UI page endpoints and API documentation.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound
from starlette.responses import RedirectResponse

import dango
from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission

router = APIRouter(tags=["ui"])

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Static asset cache busting (BUG-066): compute MD5 content hashes at import time.
_static_dir = Path(__file__).parent.parent / "static"
_static_hashes: dict[str, str] = {}
if _static_dir.exists():
    for _file in _static_dir.rglob("*"):
        if _file.is_file():
            _hash = hashlib.md5(_file.read_bytes(), usedforsecurity=False).hexdigest()[:8]
            _static_hashes[_file.relative_to(_static_dir).as_posix()] = _hash


def _static_url(path: str) -> str:
    """Return versioned static URL for cache busting."""
    h = _static_hashes.get(path)
    if h:
        return f"/static/{path}?v={h}"
    return f"/static/{path}"


templates.env.globals["static_url"] = _static_url

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


def _render_template(
    request: Request,
    template_name: str,
    context: dict,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a Jinja2 template with fallback for broken installations."""
    if "is_cloud" not in context:
        try:
            from dango.web.helpers import is_cloud_deployment

            project_root = Path(request.app.state.project_root)
            context["is_cloud"] = is_cloud_deployment(project_root)
        except Exception:
            context["is_cloud"] = False
    if "is_cloud_server" not in context:
        try:
            from dango.config.helpers import is_running_on_cloud

            context["is_cloud_server"] = is_running_on_cloud()
        except Exception:
            context["is_cloud_server"] = False
    if "query_hash" not in context:
        try:
            import base64
            import json

            project_root = Path(request.app.state.project_root)
            mb_yml = project_root / ".dango" / "metabase.yml"
            db_id = None
            if mb_yml.exists():
                mb_cfg = yaml.safe_load(mb_yml.read_text(encoding="utf-8"))
                db_id = (mb_cfg or {}).get("database", {}).get("id")
            context["query_hash"] = base64.b64encode(
                json.dumps(
                    {
                        "dataset_query": {
                            "lib/type": "mbql/query",
                            "database": db_id,
                            "stages": [
                                {
                                    "lib/type": "mbql.stage/native",
                                    "native": "",
                                    "template-tags": {},
                                }
                            ],
                        },
                        "display": "table",
                        "visualization_settings": {},
                        "type": "question",
                    },
                    separators=(",", ":"),
                ).encode()
            ).decode()
        except Exception:
            context["query_hash"] = ""
    try:
        return templates.TemplateResponse(
            request, template_name, context=context, status_code=status_code
        )
    except TemplateNotFound:
        return HTMLResponse(content=_FALLBACK_HTML)  # always 200 — this is an install error


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


@router.get("/query")
async def query_redirect(request: Request) -> HTMLResponse:
    """Redirect /query to Metabase native SQL editor with database pre-selected.

    Uses client-side redirect because browsers strip hash fragments from
    server-side 302 redirects.
    """
    target_url = "/metabase/"
    try:
        project_root = Path(request.app.state.project_root)
        metabase_yml = project_root / ".dango" / "metabase.yml"
        if metabase_yml.exists():
            metabase_config = yaml.safe_load(metabase_yml.read_text(encoding="utf-8"))
            database_id = (metabase_config or {}).get("database", {}).get("id")
            if database_id:
                import base64
                import json

                query_state = json.dumps(
                    {
                        "dataset_query": {
                            "lib/type": "mbql/query",
                            "database": database_id,
                            "stages": [
                                {
                                    "lib/type": "mbql.stage/native",
                                    "native": "",
                                    "template-tags": {},
                                }
                            ],
                        },
                        "display": "table",
                        "visualization_settings": {},
                        "type": "question",
                    },
                    separators=(",", ":"),
                )
                encoded = base64.b64encode(query_state.encode()).decode()
                target_url = f"/metabase/question#{encoded}"
    except Exception:
        pass
    # JavaScript redirect — meta refresh and 302 both strip hash fragments
    return HTMLResponse(
        f"<html><head><script>window.location.replace('{target_url}');</script></head>"
        f'<body>Redirecting to <a href="{target_url}">SQL Editor</a>...</body></html>'
    )


@router.get("/api")
async def api_info() -> dict[str, str]:
    """API information endpoint."""
    return {
        "message": "Dango API",
        "version": dango.__version__,
        "docs": "/api/docs",
        "websocket": "/ws",
    }


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
async def login_page(request: Request) -> Response:
    """Render the login page."""
    # /login is a public route (skips auth middleware), so request.state.user
    # is always None.  Validate the session cookie directly to redirect
    # already-authenticated users back to the home page.
    try:
        from dango.auth.admin import get_auth_db_path, is_auth_enabled

        project_root = request.app.state.project_root
        if is_auth_enabled(project_root):
            token = request.cookies.get("dango_session")
            if token:
                from dango.auth.sessions import validate_session

                user = validate_session(get_auth_db_path(project_root), token)
                if user is not None:
                    return RedirectResponse(url="/", status_code=302)
    except Exception:
        pass  # Fall through to login page on any error
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
