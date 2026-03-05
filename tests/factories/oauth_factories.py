"""tests/factories/oauth_factories.py

Factory functions for creating test OAuth credentials.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from dango.oauth.storage import OAuthCredential


def make_oauth_credential(
    source_type: str = "google_sheets",
    provider: str = "google",
    **overrides: Any,
) -> OAuthCredential:
    """Create a valid OAuthCredential with sensible defaults."""
    defaults: dict[str, Any] = {
        "source_type": source_type,
        "provider": provider,
        "identifier": "test@example.com",
        "account_info": "Test Account",
        "credentials": {
            "client_id": "test-client-id.apps.googleusercontent.com",
            "client_secret": "test-client-secret",
            "refresh_token": "test-refresh-token",
        },
        "created_at": datetime(2026, 1, 1),
        "expires_at": None,
        "last_refreshed": None,
        "metadata": None,
    }
    defaults.update(overrides)
    return OAuthCredential(**defaults)


def make_google_credential(**overrides: Any) -> OAuthCredential:
    """Create a Google OAuth credential."""
    defaults: dict[str, Any] = {
        "source_type": "google_sheets",
        "provider": "google",
        "identifier": "user@gmail.com",
        "account_info": "user@gmail.com",
        "credentials": {
            "client_id": "123456.apps.googleusercontent.com",
            "client_secret": "GOCSPX-secret",
            "refresh_token": "1//0abc-refresh-token",
        },
    }
    defaults.update(overrides)
    return make_oauth_credential(**defaults)


def make_facebook_credential(**overrides: Any) -> OAuthCredential:
    """Create a Facebook OAuth credential."""
    defaults: dict[str, Any] = {
        "source_type": "facebook_ads",
        "provider": "facebook",
        "identifier": "123456789",
        "account_info": "Facebook Ads Account (123456789)",
        "credentials": {
            "access_token": "EAABsbCS1IXXZD-long-lived-token",
        },
        "expires_at": datetime.now() + timedelta(days=55),
    }
    defaults.update(overrides)
    return make_oauth_credential(**defaults)


def make_shopify_credential(**overrides: Any) -> OAuthCredential:
    """Create a Shopify OAuth credential."""
    defaults: dict[str, Any] = {
        "source_type": "shopify",
        "provider": "shopify",
        "identifier": "mystore",
        "account_info": "My Store (mystore.myshopify.com)",
        "credentials": {
            "private_app_password": "shpat_abcdef123456",
            "shop_url": "mystore.myshopify.com",
        },
    }
    defaults.update(overrides)
    return make_oauth_credential(**defaults)
