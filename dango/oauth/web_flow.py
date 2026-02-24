"""dango/oauth/web_flow.py

OAuth token exchange helpers for the web-based OAuth flow.

Provides pure functions for building authorization URLs and exchanging
authorization codes for tokens.  Used by the web OAuth connect routes
(``web/routes/oauth_connect.py``) to handle browser-based OAuth for
cloud-deployed data sources.

These functions are intentionally decoupled from the CLI OAuth flow
(``providers.py``) — they contain no Rich console output or file I/O.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import requests

from dango.logging import get_logger

logger = get_logger(__name__)


class OAuthFlowError(Exception):
    """Raised when an OAuth token exchange or user info request fails."""

    def __init__(self, message: str, provider: str = "") -> None:
        super().__init__(message)
        self.provider = provider


# ---------------------------------------------------------------------------
# Supported sources
# ---------------------------------------------------------------------------

SUPPORTED_OAUTH_SOURCES: dict[str, str] = {
    "google_ads": "google",
    "google_analytics": "google",
    "google_sheets": "google",
    "facebook_ads": "facebook",
}
"""Maps dlt source_type to OAuth provider name."""


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_GOOGLE_BASE_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
]

_GOOGLE_SERVICE_SCOPES: dict[str, list[str]] = {
    "google_ads": ["https://www.googleapis.com/auth/adwords"],
    "google_analytics": ["https://www.googleapis.com/auth/analytics.readonly"],
    "google_sheets": ["https://www.googleapis.com/auth/spreadsheets.readonly"],
}


def build_google_auth_url(
    client_id: str,
    redirect_uri: str,
    source_type: str,
    state: str,
) -> str:
    """Build a Google OAuth authorization URL for a specific data source.

    Args:
        client_id: Google OAuth client ID.
        redirect_uri: Callback URL registered in Google Cloud Console.
        source_type: dlt source type (e.g. ``"google_ads"``).
        state: CSRF state token.

    Returns:
        Full authorization URL to redirect the user to.
    """
    scopes = _GOOGLE_BASE_SCOPES + _GOOGLE_SERVICE_SCOPES.get(source_type, [])
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_google_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Exchange a Google authorization code for tokens.

    Args:
        code: Authorization code from the OAuth callback.
        client_id: Google OAuth client ID.
        client_secret: Google OAuth client secret.
        redirect_uri: The same redirect URI used in the authorization request.

    Returns:
        Token response dict containing ``access_token``, ``refresh_token``,
        ``expires_in``, etc.

    Raises:
        OAuthFlowError: If the token exchange fails.
    """
    try:
        resp = requests.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise OAuthFlowError(
            f"Failed to contact Google token endpoint: {exc}",
            provider="google",
        ) from exc

    if resp.status_code != 200:
        logger.warning(
            "google_token_exchange_failed",
            status=resp.status_code,
            body=resp.text[:500],
        )
        raise OAuthFlowError(
            "Google token exchange failed. Please try again.",
            provider="google",
        )

    result: dict[str, Any] = resp.json()
    return result


def fetch_google_user_info(access_token: str) -> dict[str, Any]:
    """Fetch Google user info (email, name) using an access token.

    Args:
        access_token: A valid Google OAuth access token.

    Returns:
        User info dict containing ``email``, ``name``, etc.

    Raises:
        OAuthFlowError: If the request fails.
    """
    try:
        resp = requests.get(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise OAuthFlowError(
            f"Failed to fetch Google user info: {exc}",
            provider="google",
        ) from exc

    if resp.status_code != 200:
        raise OAuthFlowError(
            "Failed to fetch Google user info.",
            provider="google",
        )

    result: dict[str, Any] = resp.json()
    return result


# ---------------------------------------------------------------------------
# Facebook OAuth
# ---------------------------------------------------------------------------

_FACEBOOK_AUTH_URL = "https://www.facebook.com/v18.0/dialog/oauth"
_FACEBOOK_TOKEN_URL = "https://graph.facebook.com/v18.0/oauth/access_token"


def build_facebook_auth_url(
    client_id: str,
    redirect_uri: str,
    state: str,
) -> str:
    """Build a Facebook OAuth authorization URL.

    Args:
        client_id: Facebook App ID.
        redirect_uri: Callback URL registered in Facebook Developer Portal.
        state: CSRF state token.

    Returns:
        Full authorization URL to redirect the user to.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": "ads_read,ads_management",
        "response_type": "code",
    }
    return f"{_FACEBOOK_AUTH_URL}?{urlencode(params)}"


def exchange_facebook_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Exchange a Facebook authorization code for a long-lived token.

    First exchanges the code for a short-lived token, then exchanges the
    short-lived token for a long-lived token (~60 days).

    Args:
        code: Authorization code from the OAuth callback.
        client_id: Facebook App ID.
        client_secret: Facebook App Secret.
        redirect_uri: The same redirect URI used in the authorization request.

    Returns:
        Long-lived token response dict containing ``access_token``,
        ``token_type``, ``expires_in``.

    Raises:
        OAuthFlowError: If either token exchange fails.
    """
    # Step 1: Exchange code for short-lived token
    try:
        resp = requests.get(
            _FACEBOOK_TOKEN_URL,
            params={
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise OAuthFlowError(
            f"Failed to contact Facebook token endpoint: {exc}",
            provider="facebook",
        ) from exc

    if resp.status_code != 200:
        logger.warning(
            "facebook_token_exchange_failed",
            status=resp.status_code,
            body=resp.text[:500],
        )
        raise OAuthFlowError(
            "Facebook token exchange failed. Please try again.",
            provider="facebook",
        )

    short_lived: dict[str, Any] = resp.json()
    short_token = short_lived.get("access_token")
    if not short_token:
        raise OAuthFlowError(
            "Facebook did not return an access token.",
            provider="facebook",
        )

    # Step 2: Exchange short-lived token for long-lived token
    try:
        resp2 = requests.get(
            _FACEBOOK_TOKEN_URL,
            params={
                "grant_type": "fb_exchange_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "fb_exchange_token": short_token,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise OAuthFlowError(
            f"Failed to exchange for long-lived Facebook token: {exc}",
            provider="facebook",
        ) from exc

    if resp2.status_code != 200:
        logger.warning(
            "facebook_long_lived_exchange_failed",
            status=resp2.status_code,
            body=resp2.text[:500],
        )
        raise OAuthFlowError(
            "Failed to obtain long-lived Facebook token.",
            provider="facebook",
        )

    result: dict[str, Any] = resp2.json()
    return result
