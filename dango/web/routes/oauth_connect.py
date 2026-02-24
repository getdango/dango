"""dango/web/routes/oauth_connect.py

Web-based OAuth connect/callback routes for cloud deployments.

Handles browser-based OAuth flows for data sources that require OAuth
(Google Ads, Google Analytics, Google Sheets, Facebook Ads).  The CLI's
local callback server cannot be used on headless cloud servers, so these
routes provide an equivalent flow through the admin web UI.

All endpoints require ``config.manage`` permission (admin-only).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.logging import get_logger
from dango.oauth.web_flow import (
    SUPPORTED_OAUTH_SOURCES,
    OAuthFlowError,
    build_facebook_auth_url,
    build_google_auth_url,
    exchange_facebook_code,
    exchange_google_code,
    fetch_google_user_info,
)

router = APIRouter(tags=["oauth-connect"])
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_STATE_COOKIE = "dango_source_oauth_state"
_SOURCE_COOKIE = "dango_source_oauth_type"
_STATE_MAX_AGE = 600  # 10 minutes


def _get_client_ip(request: Request) -> str | None:
    """Extract client IP address from the request."""
    if request.client is not None:
        return request.client.host
    return None


def _get_redirect_uri(domain: str, source_type: str) -> str:
    """Build the OAuth callback URI for a given source type."""
    return f"https://{domain}/oauth/callback/{source_type}"


def _get_client_credentials(project_root: Path, provider: str) -> tuple[str, str] | None:
    """Read OAuth client credentials from the project ``.env`` file.

    Returns:
        Tuple of ``(client_id, client_secret)`` or ``None`` if not configured.
    """
    from dango.web.routes.secrets import _read_env_file

    env_vars = _read_env_file(project_root)

    if provider == "google":
        client_id = env_vars.get("GOOGLE_CLIENT_ID", "")
        client_secret = env_vars.get("GOOGLE_CLIENT_SECRET", "")
    elif provider == "facebook":
        client_id = env_vars.get("FACEBOOK_APP_ID", "")
        client_secret = env_vars.get("FACEBOOK_APP_SECRET", "")
    else:
        return None

    if not client_id or not client_secret:
        return None

    return client_id, client_secret


def _load_cloud_domain(project_root: Path) -> str | None:
    """Load the configured domain from cloud config, if any."""
    try:
        from dango.config.loader import ConfigLoader

        loader = ConfigLoader(project_root)
        cloud_cfg = loader.load_cloud_config()
        if cloud_cfg is not None:
            return cloud_cfg.domain
    except Exception:
        logger.debug("cloud_domain_not_available", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# GET /oauth/connect/{source_type} — initiate OAuth flow
# ---------------------------------------------------------------------------


@router.get("/oauth/connect/{source_type}", response_model=None)
async def oauth_connect(
    source_type: str,
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> RedirectResponse | JSONResponse:
    """Initiate an OAuth flow for a data source.

    Validates prerequisites (domain, client credentials), builds the
    authorization URL, sets CSRF state cookies, and redirects the user
    to the OAuth provider.
    """
    # Validate source type
    provider = SUPPORTED_OAUTH_SOURCES.get(source_type)
    if provider is None:
        return JSONResponse(
            status_code=400,
            content={
                "message": f"Unsupported OAuth source: '{source_type}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_OAUTH_SOURCES))}."
            },
        )

    project_root: Path = request.app.state.project_root

    # Check domain is configured (HTTPS required for OAuth)
    domain = _load_cloud_domain(project_root)
    if not domain:
        return JSONResponse(
            status_code=400,
            content={
                "message": "A custom domain with HTTPS is required for OAuth. "
                "Configure a domain first via the deployment settings."
            },
        )

    # Check client credentials exist
    creds = _get_client_credentials(project_root, provider)
    if creds is None:
        env_keys = {
            "google": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
            "facebook": ("FACEBOOK_APP_ID", "FACEBOOK_APP_SECRET"),
        }
        keys = env_keys.get(provider, ("CLIENT_ID", "CLIENT_SECRET"))
        return JSONResponse(
            status_code=400,
            content={
                "message": f"OAuth client credentials not configured. "
                f"Set {keys[0]} and {keys[1]} in the Secrets page first."
            },
        )

    client_id, client_secret = creds
    redirect_uri = _get_redirect_uri(domain, source_type)

    # Generate CSRF state token
    state = secrets.token_urlsafe(32)

    # Build authorization URL
    if provider == "google":
        auth_url = build_google_auth_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            source_type=source_type,
            state=state,
        )
    elif provider == "facebook":
        auth_url = build_facebook_auth_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
        )
    else:
        return JSONResponse(
            status_code=400,
            content={"message": f"Provider '{provider}' not implemented."},
        )

    # Set state cookies and redirect
    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(
        key=_STATE_COOKIE,
        value=state,
        max_age=_STATE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=True,
    )
    response.set_cookie(
        key=_SOURCE_COOKIE,
        value=source_type,
        max_age=_STATE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=True,
    )
    return response


# ---------------------------------------------------------------------------
# GET /oauth/callback/{source_type} — handle provider redirect
# ---------------------------------------------------------------------------


@router.get("/oauth/callback/{source_type}")
async def oauth_callback(
    source_type: str,
    request: Request,
    user: User = Depends(require_permission("config.manage")),
) -> RedirectResponse:
    """Handle OAuth provider callback.

    Validates the CSRF state, exchanges the authorization code for
    tokens, saves credentials via ``OAuthStorage``, logs an audit
    event, and redirects back to the secrets page.
    """
    secrets_url = "/settings/secrets"

    # Verify state parameter (CSRF protection)
    expected_state = request.cookies.get(_STATE_COOKIE)
    actual_state = request.query_params.get("state")
    if not expected_state or expected_state != actual_state:
        logger.warning(
            "oauth_state_mismatch",
            source_type=source_type,
            has_expected=bool(expected_state),
            has_actual=bool(actual_state),
        )
        return RedirectResponse(url=f"{secrets_url}?error=state_mismatch")

    # Verify source type cookie matches path
    cookie_source = request.cookies.get(_SOURCE_COOKIE)
    if cookie_source != source_type:
        logger.warning(
            "oauth_source_mismatch",
            path_source=source_type,
            cookie_source=cookie_source,
        )
        return RedirectResponse(url=f"{secrets_url}?error=state_mismatch")

    # Check for error from provider
    error = request.query_params.get("error")
    if error:
        logger.warning("oauth_provider_error", source_type=source_type, error=error)
        return RedirectResponse(url=f"{secrets_url}?error=provider_denied")

    # Get authorization code
    code = request.query_params.get("code")
    if not code:
        return RedirectResponse(url=f"{secrets_url}?error=no_code")

    provider = SUPPORTED_OAUTH_SOURCES.get(source_type)
    if provider is None:
        return RedirectResponse(url=f"{secrets_url}?error=unsupported_source")

    project_root: Path = request.app.state.project_root

    # Load domain and client credentials
    domain = _load_cloud_domain(project_root)
    if not domain:
        return RedirectResponse(url=f"{secrets_url}?error=no_domain")

    creds = _get_client_credentials(project_root, provider)
    if creds is None:
        return RedirectResponse(url=f"{secrets_url}?error=no_credentials")

    client_id, client_secret = creds
    redirect_uri = _get_redirect_uri(domain, source_type)

    # Exchange code for tokens
    try:
        token_data = _exchange_code(
            provider=provider,
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
    except OAuthFlowError as exc:
        logger.warning(
            "oauth_token_exchange_failed",
            source_type=source_type,
            error=str(exc),
        )
        return RedirectResponse(url=f"{secrets_url}?error=token_exchange_failed")

    # Fetch user info (Google only) for account identification
    identifier = ""
    account_info = ""
    if provider == "google":
        try:
            user_info: dict[str, Any] = fetch_google_user_info(token_data.get("access_token", ""))
            identifier = user_info.get("email", "")
            account_info = user_info.get("name", identifier)
        except OAuthFlowError:
            logger.debug("oauth_user_info_failed", source_type=source_type)

    # Save credentials via OAuthStorage
    try:
        from dango.oauth.storage import OAuthCredential, OAuthStorage

        storage = OAuthStorage(project_root)

        # Build credential dict
        credential_data: dict[str, Any] = {
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if provider == "google":
            credential_data["refresh_token"] = token_data.get("refresh_token", "")
            credential_data["project_id"] = "dango-oauth"
        elif provider == "facebook":
            credential_data["access_token"] = token_data.get("access_token", "")

        expires_at = None
        expires_in = token_data.get("expires_in")
        if expires_in and isinstance(expires_in, int):
            expires_at = datetime.now() + timedelta(seconds=expires_in)

        oauth_cred = OAuthCredential(
            source_type=source_type,
            provider=provider,
            identifier=identifier,
            account_info=account_info,
            credentials=credential_data,
            created_at=datetime.now(),
            expires_at=expires_at,
        )
        storage.save(oauth_cred)
    except Exception:
        logger.error("oauth_credential_save_failed", source_type=source_type, exc_info=True)
        return RedirectResponse(url=f"{secrets_url}?error=save_failed")

    # Log audit event
    log_auth_event(
        AuditEvent.OAUTH_SOURCE_CONNECTED,
        user_id=user.id,
        email=user.email,
        ip=_get_client_ip(request),
        details={"source_type": source_type, "provider": provider, "identifier": identifier},
    )

    # Clear state cookies and redirect to secrets page
    response = RedirectResponse(url=f"{secrets_url}?connected={source_type}")
    response.delete_cookie(_STATE_COOKIE)
    response.delete_cookie(_SOURCE_COOKIE)
    return response


# ---------------------------------------------------------------------------
# Token exchange dispatcher
# ---------------------------------------------------------------------------


def _exchange_code(
    provider: str,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Dispatch token exchange to the correct provider helper.

    Returns:
        Token response dict.

    Raises:
        OAuthFlowError: If the exchange fails.
    """
    if provider == "google":
        return exchange_google_code(
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
    if provider == "facebook":
        return exchange_facebook_code(
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
    raise OAuthFlowError(f"Unsupported provider: {provider}", provider=provider)
