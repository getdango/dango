"""dango/oauth/validation.py

Live token validation for OAuth credentials.

Makes lightweight API calls to verify tokens actually work, rather than
just checking stored metadata (expiry dates). Used by ``dango auth check``,
pre-sync validation, and ``dango status``.

Network error policy: if validation fails due to network issues
(ConnectionError, Timeout), the token is given the benefit of the doubt
(``valid=True``, ``error_code="network_error"``). Pre-sync validation
does NOT raise on network errors — don't block sync just because a
provider API is momentarily unreachable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from dango.exceptions import OAuthTokenExpiredError, OAuthTokenRevokedError
from dango.oauth.router import OAUTH_PROVIDER_MAP
from dango.oauth.storage import OAuthCredential, OAuthStorage

logger = logging.getLogger(__name__)

# Google OAuth token endpoint
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v1/userinfo"

# Facebook Graph API
_FACEBOOK_ME_URL = "https://graph.facebook.com/me"

# Shopify API version (matches providers.py)
_SHOPIFY_API_VERSION = "2024-01"

# Request timeout in seconds
_REQUEST_TIMEOUT = 10


@dataclass
class TokenValidationResult:
    """Result of a live token validation check."""

    source_type: str
    provider: str
    valid: bool
    message: str
    account_info: str = ""
    expires_at: datetime | None = None
    days_until_expiry: int | None = None
    error_code: str | None = (
        None  # "revoked", "expired", "network_error", "server_error", "missing_credentials"
    )


def validate_google_token(credential: OAuthCredential) -> TokenValidationResult:
    """Validate a Google OAuth credential by exchanging the refresh token.

    1. POST refresh_token to Google's token endpoint to get a fresh access_token.
    2. GET /oauth2/v1/userinfo with the access_token to verify it works.

    Args:
        credential: An OAuthCredential with Google credentials.

    Returns:
        TokenValidationResult with validation outcome.
    """
    creds = credential.credentials
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")
    refresh_token = creds.get("refresh_token")

    if not all([client_id, client_secret, refresh_token]):
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=False,
            message="Missing credentials (client_id, client_secret, or refresh_token)",
            error_code="missing_credentials",
        )

    try:
        # Step 1: Exchange refresh_token for access_token
        token_response = requests.post(
            _GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=_REQUEST_TIMEOUT,
        )

        if token_response.status_code != 200:
            # Server errors (5xx) get benefit of the doubt
            if token_response.status_code >= 500:
                return TokenValidationResult(
                    source_type=credential.source_type,
                    provider=credential.provider,
                    valid=True,
                    message="Google API returned a server error",
                    account_info=credential.account_info,
                    error_code="server_error",
                )
            try:
                error_data: dict[str, Any] = token_response.json()
            except (ValueError, requests.JSONDecodeError):
                error_data = {}
            error_type = error_data.get("error", "unknown")
            if error_type == "invalid_grant":
                return TokenValidationResult(
                    source_type=credential.source_type,
                    provider=credential.provider,
                    valid=False,
                    message=f"Token revoked. Re-authenticate: dango auth {credential.source_type}",
                    error_code="revoked",
                )
            return TokenValidationResult(
                source_type=credential.source_type,
                provider=credential.provider,
                valid=False,
                message=f"Token exchange failed: {error_type}",
                error_code="revoked",
            )

        try:
            token_data: dict[str, Any] = token_response.json()
        except (ValueError, requests.JSONDecodeError):
            return TokenValidationResult(
                source_type=credential.source_type,
                provider=credential.provider,
                valid=True,
                message="Could not parse token response",
                account_info=credential.account_info,
                error_code="server_error",
            )
        access_token = token_data.get("access_token")

        if not access_token:
            return TokenValidationResult(
                source_type=credential.source_type,
                provider=credential.provider,
                valid=False,
                message="Token exchange succeeded but no access_token returned",
                error_code="revoked",
            )

        # Step 2: Verify access_token with userinfo endpoint
        userinfo_response = requests.get(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_REQUEST_TIMEOUT,
        )

        if userinfo_response.status_code >= 500:
            return TokenValidationResult(
                source_type=credential.source_type,
                provider=credential.provider,
                valid=True,
                message="Google userinfo API returned a server error",
                account_info=credential.account_info,
                error_code="server_error",
            )

        if userinfo_response.status_code != 200:
            return TokenValidationResult(
                source_type=credential.source_type,
                provider=credential.provider,
                valid=False,
                message="Access token verification failed",
                error_code="revoked",
            )

        userinfo: dict[str, Any] = userinfo_response.json()
        email = userinfo.get("email", "")

        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=True,
            message="Token valid",
            account_info=email,
            expires_at=credential.expires_at,
            days_until_expiry=credential.days_until_expiry(),
        )

    except (requests.ConnectionError, requests.Timeout) as exc:
        logger.debug(
            "Network error validating Google token for %s: %s", credential.source_type, exc
        )
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=True,
            message="Could not reach Google API (network error)",
            account_info=credential.account_info,
            error_code="network_error",
        )


def validate_facebook_token(credential: OAuthCredential) -> TokenValidationResult:
    """Validate a Facebook OAuth credential.

    Checks stored expiry first, then calls GET /me to verify the token.

    Args:
        credential: An OAuthCredential with Facebook credentials.

    Returns:
        TokenValidationResult with validation outcome.
    """
    creds = credential.credentials
    access_token = creds.get("access_token")

    if not access_token:
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=False,
            message="Missing access_token",
            error_code="missing_credentials",
        )

    # Check stored expiry first
    if credential.is_expired():
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=False,
            message=f"Token expired. Re-authenticate: dango auth {credential.source_type}",
            expires_at=credential.expires_at,
            error_code="expired",
        )

    try:
        response = requests.get(
            _FACEBOOK_ME_URL,
            params={"access_token": access_token},
            timeout=_REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            data: dict[str, Any] = response.json()
            name = data.get("name", "")
            return TokenValidationResult(
                source_type=credential.source_type,
                provider=credential.provider,
                valid=True,
                message="Token valid",
                account_info=name or credential.account_info,
                expires_at=credential.expires_at,
                days_until_expiry=credential.days_until_expiry(),
            )

        # Server errors (5xx) get benefit of the doubt
        if response.status_code >= 500:
            return TokenValidationResult(
                source_type=credential.source_type,
                provider=credential.provider,
                valid=True,
                message="Facebook API returned a server error",
                account_info=credential.account_info,
                error_code="server_error",
            )

        # 401 or client error means revoked/invalid
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=False,
            message=f"Token revoked. Re-authenticate: dango auth {credential.source_type}",
            error_code="revoked",
        )

    except (requests.ConnectionError, requests.Timeout) as exc:
        logger.debug(
            "Network error validating Facebook token for %s: %s", credential.source_type, exc
        )
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=True,
            message="Could not reach Facebook API (network error)",
            account_info=credential.account_info,
            error_code="network_error",
        )


def validate_shopify_token(credential: OAuthCredential) -> TokenValidationResult:
    """Validate a Shopify credential by calling the shop info endpoint.

    Args:
        credential: An OAuthCredential with Shopify credentials.

    Returns:
        TokenValidationResult with validation outcome.
    """
    creds = credential.credentials
    access_token = creds.get("private_app_password")
    shop_url = creds.get("shop_url")

    if not access_token or not shop_url:
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=False,
            message="Missing credentials (private_app_password or shop_url)",
            error_code="missing_credentials",
        )

    try:
        url = f"https://{shop_url}/admin/api/{_SHOPIFY_API_VERSION}/shop.json"
        response = requests.get(
            url,
            headers={"X-Shopify-Access-Token": access_token},
            timeout=_REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            shop_data: dict[str, Any] = response.json()
            shop_name = shop_data.get("shop", {}).get("name", "")
            return TokenValidationResult(
                source_type=credential.source_type,
                provider=credential.provider,
                valid=True,
                message="Token valid",
                account_info=f"{shop_name} ({shop_url})" if shop_name else shop_url,
            )

        # Server errors (5xx) get benefit of the doubt
        if response.status_code >= 500:
            return TokenValidationResult(
                source_type=credential.source_type,
                provider=credential.provider,
                valid=True,
                message="Shopify API returned a server error",
                account_info=credential.account_info,
                error_code="server_error",
            )

        # 401 or client error means revoked/invalid
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=False,
            message="Token invalid. Re-authenticate: dango auth shopify",
            error_code="revoked",
        )

    except (requests.ConnectionError, requests.Timeout) as exc:
        logger.debug(
            "Network error validating Shopify token for %s: %s", credential.source_type, exc
        )
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=True,
            message="Could not reach Shopify API (network error)",
            account_info=credential.account_info,
            error_code="network_error",
        )


# Provider → validator mapping
_PROVIDER_VALIDATORS = {
    "google": validate_google_token,
    "facebook": validate_facebook_token,
    "facebook_ads": validate_facebook_token,
    "shopify": validate_shopify_token,
}


def validate_token(credential: OAuthCredential) -> TokenValidationResult:
    """Validate a single OAuth credential by routing to the correct provider validator.

    Args:
        credential: The OAuth credential to validate.

    Returns:
        TokenValidationResult with validation outcome.
    """
    validator = _PROVIDER_VALIDATORS.get(credential.provider)
    if validator is None:
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=True,
            message=f"No validator for provider '{credential.provider}'",
            account_info=credential.account_info,
        )
    return validator(credential)


def validate_all_tokens(project_root: Path) -> list[TokenValidationResult]:
    """Validate all stored OAuth tokens with live API calls.

    Args:
        project_root: Root of the Dango project.

    Returns:
        List of TokenValidationResult for each stored credential.
    """
    storage = OAuthStorage(project_root)
    credentials = storage.list()
    results: list[TokenValidationResult] = []
    for cred in credentials:
        result = validate_token(cred)
        results.append(result)
    return results


def validate_before_sync(source_type: str, project_root: Path) -> None:
    """Pre-sync gate: validate OAuth token if the source uses OAuth.

    For non-OAuth sources, returns immediately. For OAuth sources, validates
    the token and raises if revoked/expired. Network errors are silently
    ignored (benefit of the doubt).

    Args:
        source_type: The source type value (e.g. "google_sheets").
        project_root: Root of the Dango project.

    Raises:
        OAuthTokenRevokedError: If the token is revoked.
        OAuthTokenExpiredError: If the token is expired.
    """
    # Only validate OAuth sources
    if source_type not in OAUTH_PROVIDER_MAP:
        return

    storage = OAuthStorage(project_root)
    credential = storage.get(source_type)

    if credential is None:
        # No stored credential — let dlt handle the missing credentials error
        return

    result = validate_token(credential)

    # Silent pass on network errors and server errors
    if result.error_code in ("network_error", "server_error"):
        return

    if not result.valid:
        if result.error_code == "expired":
            raise OAuthTokenExpiredError(
                result.message,
                user_message=result.message,
                context={"source_type": source_type, "provider": result.provider},
            )
        if result.error_code == "missing_credentials":
            msg = (
                f"Incomplete OAuth credentials for {source_type}. "
                f"Re-authenticate: dango auth {source_type}"
            )
            raise OAuthTokenRevokedError(
                msg,
                user_message=msg,
                context={"source_type": source_type, "provider": result.provider},
            )
        raise OAuthTokenRevokedError(
            result.message,
            user_message=result.message,
            context={"source_type": source_type, "provider": result.provider},
        )
