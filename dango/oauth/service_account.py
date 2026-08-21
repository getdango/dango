"""dango/oauth/service_account.py

Service account authentication for Google Cloud sources.

Handles Google service account key validation, storage, and verification.
Service accounts use JWT signing (private_key) instead of OAuth refresh tokens.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from dango.oauth.storage import OAuthCredential, OAuthStorage

if TYPE_CHECKING:
    from dango.oauth.validation import TokenValidationResult

logger = logging.getLogger(__name__)


def validate_service_account_json(key_data: dict[str, Any]) -> tuple[bool, str]:
    """Validate service account JSON structure and format.

    Args:
        key_data: Parsed service account JSON dict.

    Returns:
        Tuple of (is_valid, error_message).
    """
    required_fields = {"type", "project_id", "private_key", "client_email"}
    missing = required_fields - key_data.keys()
    if missing:
        return False, f"Missing required fields: {', '.join(sorted(missing))}"

    if key_data.get("type") != "service_account":
        return False, f"Expected type 'service_account', got '{key_data.get('type')}'"

    # Validate private key format (should be PEM-encoded)
    private_key = key_data.get("private_key", "")
    if not isinstance(private_key, str) or not private_key.startswith("-----BEGIN"):
        return False, "Invalid private_key format (not PEM-encoded)"

    if "-----END" not in private_key:
        return False, "Invalid private_key format (incomplete PEM)"

    return True, ""


def verify_service_account_key(key_data: dict[str, Any], source_type: str) -> tuple[bool, str]:
    """Verify service account key works by making a lightweight API call.

    Args:
        key_data: Parsed service account JSON dict.
        source_type: Google source type (google_sheets, google_analytics, google_ads).

    Returns:
        Tuple of (is_valid, error_message).
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.service_account import Credentials
    except ImportError:
        return False, "google-auth not installed. Run: pip install google-auth"

    try:
        # Create service account credentials from the key data
        credentials = Credentials.from_service_account_info(
            key_data,
            scopes=_get_scopes_for_source(source_type),
        )

        # Make a lightweight request to verify the key works
        request = Request()
        credentials.refresh(request)

        return True, ""

    except ValueError as e:
        return False, f"Invalid service account key: {str(e)}"
    except Exception as e:
        return False, f"Key verification failed: {str(e)}"


def _get_scopes_for_source(source_type: str) -> list[str]:
    """Get the required OAuth scopes for a Google source type.

    Args:
        source_type: Google source type (google_sheets, google_analytics, google_ads).

    Returns:
        List of OAuth scopes required for the source.
    """
    scopes_map = {
        "google_sheets": [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
        ],
        "google_analytics": [
            "https://www.googleapis.com/auth/analytics.readonly",
        ],
        "google_ads": [
            "https://www.googleapis.com/auth/adwords",
        ],
    }
    return scopes_map.get(source_type, ["https://www.googleapis.com/auth/cloud-platform"])


def save_service_account_credential(
    key_data: dict[str, Any],
    source_type: str,
    project_root: Path,
) -> tuple[bool, str]:
    """Save a validated service account credential.

    Args:
        key_data: Parsed and validated service account JSON.
        source_type: Google source type (google_sheets, google_analytics, google_ads).
        project_root: Project root directory.

    Returns:
        Tuple of (success, error_message).
    """
    try:
        oauth_storage = OAuthStorage(project_root)

        # Create credential with service account format
        credential = OAuthCredential(
            source_type=source_type,
            provider="google_service_account",
            identifier=key_data["client_email"],
            account_info=key_data["client_email"],
            credentials=key_data,  # Store full key data (storage will extract what's needed)
            created_at=datetime.now(tz=timezone.utc),
            metadata={
                "auth_method": "service_account",
                "key_path": None,
                "project_id": key_data["project_id"],
                "client_email": key_data["client_email"],
            },
        )

        # Save the credential
        if not oauth_storage.save(credential):
            return False, "Failed to save credential to storage"

        return True, ""

    except Exception as e:
        return False, f"Error saving credential: {str(e)}"


def get_service_account_credential(
    source_type: str, project_root: Path
) -> tuple[dict[str, Any] | None, str]:
    """Retrieve a saved service account credential.

    Args:
        source_type: Google source type.
        project_root: Project root directory.

    Returns:
        Tuple of (credentials_dict, error_message). credentials_dict is None if not found.
    """
    try:
        oauth_storage = OAuthStorage(project_root)
        credential = oauth_storage.get(source_type)

        if credential is None:
            return None, f"No credentials found for {source_type}"

        if credential.provider != "google_service_account":
            return None, f"Credential is not a service account ({credential.provider})"

        return credential.credentials, ""

    except Exception as e:
        return None, f"Error retrieving credential: {str(e)}"


def validate_google_service_account(credential: OAuthCredential) -> TokenValidationResult:
    """Validate a Google service account credential.

    Service accounts use JWT signing with private keys. Verification involves
    attempting to create credentials from the key data and refresh them.

    Args:
        credential: An OAuthCredential with service account key data.

    Returns:
        TokenValidationResult with validation outcome.
    """
    from dango.oauth.validation import TokenValidationResult  # lazy — avoids circular import

    creds = credential.credentials
    private_key = creds.get("private_key")
    client_email = creds.get("client_email")
    project_id = creds.get("project_id")

    if not private_key or not client_email or not project_id:
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=False,
            message="Missing service account fields (private_key, client_email, or project_id)",
            error_code="missing_credentials",
            account_info=credential.account_info,
        )

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.service_account import Credentials
    except ImportError:
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=False,
            message="google-auth not installed. Run: pip install google-auth",
            error_code="google_auth_missing",
            account_info=credential.account_info,
        )

    try:
        # Create service account credentials from the key data
        service_creds = Credentials.from_service_account_info(
            creds,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )

        # Attempt to refresh credentials to verify the key is valid
        request = Request()
        service_creds.refresh(request)

        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=True,
            message="Service account key valid",
            account_info=client_email,
        )

    except ValueError as exc:
        # Invalid key format
        logger.debug("Service account validation error for %s: %s", credential.source_type, exc)
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=False,
            message=f"Invalid service account key: {str(exc)}",
            error_code="invalid_key",
            account_info=credential.account_info,
        )

    except (requests.ConnectionError, requests.Timeout) as exc:
        logger.debug(
            "Network error validating service account for %s: %s",
            credential.source_type,
            exc,
        )
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=True,
            message="Could not reach Google API (network error)",
            error_code="network_error",
            account_info=credential.account_info,
        )

    except Exception as exc:
        logger.debug("Service account validation error for %s: %s", credential.source_type, exc)
        return TokenValidationResult(
            source_type=credential.source_type,
            provider=credential.provider,
            valid=False,
            message=f"Service account verification failed: {str(exc)}",
            error_code="verification_failed",
            account_info=credential.account_info,
        )
