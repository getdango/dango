"""dango/web/routes/telemetry.py

Web front-end onto the same unified telemetry control `dango telemetry
status/on/off` (`cli/commands/telemetry.py`) exposes on the CLI (1.0.8-U).
Admin-only: lists and toggles the opt-in/opt-out state of Dango's four
telemetry providers (dango, dbt, dlt, metabase).

This module is Level 2 (`web/`) and must never import from `cli/`
(Level 3) — the module dependency hierarchy in `dango/CLAUDE.md` only
allows imports to flow downward. The actual state read/write logic for
each provider lives at or below this level already: dbt and dlt in
`dango/telemetry.py` (Level 0), metabase in
`dango/visualization/metabase.py` (Level 2, same level as this module) —
`cli/commands/telemetry.py`'s provider helpers call the exact same
functions, so the CLI and this web page are two front-ends onto one
source of truth, not two competing implementations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import dango
from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.logging import get_logger
from dango.telemetry import (
    PROVIDERS,
    get_dbt_telemetry_state,
    get_dlt_telemetry_state,
    is_telemetry_enabled,
    set_dbt_telemetry_state,
    set_dlt_telemetry_state,
    set_telemetry_enabled,
)
from dango.visualization.metabase import get_metabase_telemetry_state, set_metabase_telemetry

router = APIRouter(tags=["telemetry"])
logger = get_logger(__name__)

# Provider metadata for the status table — labels and "what it sends" text
# mirror `cli/commands/telemetry.py`'s `telemetry_status()` table exactly,
# so the CLI and web page never disagree about what each provider does.
_PROVIDER_INFO: dict[str, dict[str, str]] = {
    "dango": {
        "label": "dango",
        "description": "UUID, version, OS, source type names",
    },
    "dbt": {
        "label": "dbt-core",
        "description": "OS, Python version, invocation success/duration",
    },
    "dlt": {
        "label": "dlt",
        "description": "Command names, hashed pipeline names, execution times",
    },
    "metabase": {
        "label": "metabase",
        "description": "Anonymous usage statistics",
    },
}


def _get_client_ip(request: Request) -> str | None:
    """Extract client IP address from the request."""
    if request.client is not None:
        return request.client.host
    return None


def _get_provider_state(provider: str, project_root: Path) -> bool:
    """Return the current enabled/disabled state for a single provider."""
    if provider == "dango":
        return is_telemetry_enabled()
    if provider == "dbt":
        return get_dbt_telemetry_state()
    if provider == "dlt":
        return get_dlt_telemetry_state(project_root)
    if provider == "metabase":
        return get_metabase_telemetry_state(project_root)
    raise ValueError(f"Unknown provider: {provider}")


def _set_provider_state(provider: str, enabled: bool, project_root: Path) -> None:
    """Write through the enabled/disabled state for a single provider.

    Raises:
        OSError: dbt or dlt write failure (~/.dango/config.yml) — dlt moved
            to this machine-level key in 1.0.8-OPS-2, matching dbt, so it
            no longer raises ValueError for a malformed project-level
            .dlt/config.toml (that file is no longer written by this code
            path at all).
        click.ClickException: Metabase API/credentials failure —
            `set_metabase_telemetry()` (dango/visualization/metabase.py)
            raises this directly rather than a plain exception (an
            existing precedent in that module, not something introduced
            here) — callers must catch it explicitly, not just
            `Exception`/`RuntimeError`.
    """
    if provider == "dango":
        set_telemetry_enabled(enabled)
    elif provider == "dbt":
        set_dbt_telemetry_state(enabled)
    elif provider == "dlt":
        set_dlt_telemetry_state(enabled)
    elif provider == "metabase":
        set_metabase_telemetry(project_root, enabled)
    else:
        raise ValueError(f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# GET /api/telemetry/status — current state for all four providers
# ---------------------------------------------------------------------------


@router.get("/api/telemetry/status")
async def get_telemetry_status(
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> JSONResponse:
    """Return current telemetry state for all four providers."""
    project_root: Path = request.app.state.project_root

    providers = [
        {
            "provider": provider,
            "label": info["label"],
            "enabled": _get_provider_state(provider, project_root),
            "description": info["description"],
        }
        for provider, info in _PROVIDER_INFO.items()
    ]

    return JSONResponse(content={"providers": providers})


# ---------------------------------------------------------------------------
# POST /api/telemetry — toggle a single provider
# ---------------------------------------------------------------------------


@router.post("/api/telemetry")
async def set_telemetry(
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> JSONResponse:
    """Enable or disable telemetry for a single provider."""
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Invalid JSON body."})

    provider = body.get("provider")
    enabled = body.get("enabled")

    if provider not in PROVIDERS:
        return JSONResponse(
            status_code=400,
            content={"message": f"Invalid provider. Must be one of: {', '.join(PROVIDERS)}."},
        )
    if not isinstance(enabled, bool):
        return JSONResponse(status_code=400, content={"message": "'enabled' must be a boolean."})

    project_root: Path = request.app.state.project_root

    try:
        _set_provider_state(provider, enabled, project_root)
    except click.ClickException as e:
        return JSONResponse(status_code=400, content={"message": e.format_message()})
    except (OSError, ValueError) as e:
        return JSONResponse(status_code=400, content={"message": str(e)})
    except Exception:
        logger.warning("telemetry_toggle_failed", provider=provider, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"message": f"Failed to update telemetry setting for '{provider}'."},
        )

    log_auth_event(
        AuditEvent.TELEMETRY_TOGGLED,
        user_id=user.id,
        email=user.email,
        ip=_get_client_ip(request),
        details={"provider": provider, "enabled": enabled},
    )

    verb = "enabled" if enabled else "disabled"
    return JSONResponse(
        status_code=200,
        content={"message": f"Telemetry {verb} for {provider}.", "provider": provider},
    )


# ---------------------------------------------------------------------------
# GET /settings/telemetry — admin telemetry settings page
# ---------------------------------------------------------------------------


@router.get("/settings/telemetry")
async def telemetry_page(
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> HTMLResponse:
    """Render the admin telemetry settings page."""
    from dango.web.routes.ui import _render_template

    return _render_template(
        request,
        "telemetry.html",
        {
            "version": dango.__version__,
            "current_page": "settings",
            "subtitle": "Telemetry",
        },
    )
