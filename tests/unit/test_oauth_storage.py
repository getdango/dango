"""tests/unit/test_oauth_storage.py

Tests for dango.oauth.storage — OAuthCredential model and OAuthStorage CRUD.

Focuses on:
- OAuthCredential expiry helpers (is_expired, is_expiring_soon, days_until_expiry)
- Shopify credential save/get round-trip (P6-002 bug fix regression)
- Facebook credential save/get round-trip (factory provider name)
- exists() incomplete fix (Shopify private_app_password not checked)
- Graceful degradation on corrupt/missing secrets
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dango.oauth.storage import OAuthStorage
from tests.factories.oauth_factories import (
    make_facebook_credential,
    make_google_credential,
    make_shopify_credential,
)

# ---------------------------------------------------------------------------
# OAuthCredential model
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOAuthCredentialExpiry:
    """Tests for OAuthCredential expiry helper methods."""

    def test_no_expiry_is_not_expired(self) -> None:
        cred = make_google_credential(expires_at=None)
        assert cred.is_expired() is False

    def test_no_expiry_days_until_expiry_none(self) -> None:
        cred = make_google_credential(expires_at=None)
        assert cred.days_until_expiry() is None

    def test_no_expiry_is_not_expiring_soon(self) -> None:
        cred = make_google_credential(expires_at=None)
        assert cred.is_expiring_soon() is False

    def test_future_expiry_not_expired(self) -> None:
        cred = make_facebook_credential(expires_at=datetime.now() + timedelta(days=30))
        assert cred.is_expired() is False

    def test_past_expiry_is_expired(self) -> None:
        cred = make_facebook_credential(expires_at=datetime.now() - timedelta(hours=1))
        assert cred.is_expired() is True

    def test_days_until_expiry_positive(self) -> None:
        cred = make_facebook_credential(expires_at=datetime.now() + timedelta(days=10))
        days = cred.days_until_expiry()
        assert days is not None
        assert days >= 9  # allow for sub-day rounding

    def test_days_until_expiry_zero_when_expired(self) -> None:
        cred = make_facebook_credential(expires_at=datetime.now() - timedelta(days=5))
        assert cred.days_until_expiry() == 0

    def test_expiring_soon_within_7_days(self) -> None:
        cred = make_facebook_credential(expires_at=datetime.now() + timedelta(days=3))
        assert cred.is_expiring_soon(days=7) is True

    def test_not_expiring_soon_beyond_threshold(self) -> None:
        cred = make_facebook_credential(expires_at=datetime.now() + timedelta(days=30))
        assert cred.is_expiring_soon(days=7) is False

    def test_expiring_soon_custom_threshold(self) -> None:
        cred = make_facebook_credential(expires_at=datetime.now() + timedelta(days=25))
        assert cred.is_expiring_soon(days=30) is True
        assert cred.is_expiring_soon(days=7) is False


# ---------------------------------------------------------------------------
# Shopify storage (P6-002 regression)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShopifyStorage:
    """Regression tests for Shopify credential retrieval (P6-002 fix).

    The P6-002 fix added ``private_app_password`` to the credential detection
    keys in ``get()`` and ``list()``.  These tests ensure the fix is not reverted.
    """

    def test_save_and_get_round_trip(self, tmp_path: Path) -> None:
        """Shopify cred saved via save() is retrievable via get()."""
        storage = OAuthStorage(tmp_path)
        cred = make_shopify_credential()
        assert storage.save(cred) is True

        loaded = storage.get("shopify")
        assert loaded is not None
        assert loaded.source_type == "shopify"
        assert loaded.provider == "shopify"
        assert loaded.credentials["private_app_password"] == "shpat_abcdef123456"
        assert loaded.credentials["shop_url"] == "mystore.myshopify.com"

    def test_list_includes_shopify(self, tmp_path: Path) -> None:
        """list() returns Shopify credentials alongside other providers."""
        storage = OAuthStorage(tmp_path)
        storage.save(make_shopify_credential())
        storage.save(make_facebook_credential())

        creds = storage.list()
        source_types = {c.source_type for c in creds}
        assert "shopify" in source_types
        assert "facebook_ads" in source_types

    def test_delete_cleans_shopify_keys(self, tmp_path: Path) -> None:
        """delete() removes Shopify credential keys from secrets.toml."""
        storage = OAuthStorage(tmp_path)
        storage.save(make_shopify_credential())
        assert storage.get("shopify") is not None

        assert storage.delete("shopify") is True
        assert storage.get("shopify") is None

    def test_list_filtered_by_provider(self, tmp_path: Path) -> None:
        """list(provider='shopify') returns only Shopify credentials."""
        storage = OAuthStorage(tmp_path)
        storage.save(make_shopify_credential())
        storage.save(make_facebook_credential())

        creds = storage.list(provider="shopify")
        assert len(creds) == 1
        assert creds[0].provider == "shopify"


# ---------------------------------------------------------------------------
# Facebook storage
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFacebookStorage:
    """Tests for Facebook credential storage and factory provider name."""

    def test_factory_provider_name(self) -> None:
        """Factory sets provider='facebook' (not 'facebook_ads')."""
        cred = make_facebook_credential()
        assert cred.provider == "facebook"
        assert cred.source_type == "facebook_ads"

    def test_save_and_get_round_trip(self, tmp_path: Path) -> None:
        """Facebook cred with access_token is retrievable via get()."""
        storage = OAuthStorage(tmp_path)
        cred = make_facebook_credential()
        assert storage.save(cred) is True

        loaded = storage.get("facebook_ads")
        assert loaded is not None
        assert loaded.provider == "facebook"
        assert "access_token" in loaded.credentials


# ---------------------------------------------------------------------------
# exists() bug — incomplete P6-002 fix
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExistsBug:
    """Document that exists() does not check private_app_password.

    The P6-002 fix updated get() and list() to detect Shopify credentials
    via ``private_app_password``, but missed updating exists().  This test
    verifies the fix once exists() is patched.
    """

    def test_exists_detects_shopify_after_fix(self, tmp_path: Path) -> None:
        """exists('shopify') returns True after save (requires fix)."""
        storage = OAuthStorage(tmp_path)
        storage.save(make_shopify_credential())
        # After the exists() fix, this should be True
        assert storage.exists("shopify") is True

    def test_exists_detects_facebook(self, tmp_path: Path) -> None:
        """exists('facebook_ads') works for access_token-based creds."""
        storage = OAuthStorage(tmp_path)
        storage.save(make_facebook_credential())
        assert storage.exists("facebook_ads") is True

    def test_exists_false_for_missing(self, tmp_path: Path) -> None:
        """exists() returns False for source with no credentials."""
        storage = OAuthStorage(tmp_path)
        assert storage.exists("nonexistent") is False


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStorageGracefulDegradation:
    """Storage operations handle corrupt or missing data gracefully."""

    def test_get_corrupt_toml_returns_none(self, tmp_path: Path) -> None:
        """Corrupt secrets.toml → get() returns None."""
        storage = OAuthStorage(tmp_path)
        storage.secrets_file.write_text("{{{{not valid toml")
        assert storage.get("google_sheets") is None

    def test_list_corrupt_toml_returns_empty(self, tmp_path: Path) -> None:
        """Corrupt secrets.toml → list() returns empty list."""
        storage = OAuthStorage(tmp_path)
        storage.secrets_file.write_text("{{{{not valid toml")
        assert storage.list() == []

    def test_get_empty_source_returns_none(self, tmp_path: Path) -> None:
        """Empty source section → get() returns None."""
        storage = OAuthStorage(tmp_path)
        storage.secrets_file.write_text("[sources.shopify]\n")
        assert storage.get("shopify") is None

    def test_init_creates_dlt_directory(self, tmp_path: Path) -> None:
        """OAuthStorage.__init__ creates .dlt directory if missing."""
        dlt_dir = tmp_path / ".dlt"
        assert not dlt_dir.exists()
        OAuthStorage(tmp_path)
        assert dlt_dir.exists()
