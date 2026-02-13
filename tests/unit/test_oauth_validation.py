"""tests/unit/test_oauth_validation.py

Tests for dango/oauth/validation.py — live token validation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import OAuthTokenExpiredError, OAuthTokenRevokedError
from dango.oauth.validation import (
    TokenValidationResult,
    validate_all_tokens,
    validate_before_sync,
    validate_facebook_token,
    validate_google_token,
    validate_shopify_token,
    validate_token,
)
from tests.factories.oauth_factories import (
    make_facebook_credential,
    make_google_credential,
    make_oauth_credential,
    make_shopify_credential,
)


@pytest.mark.unit
class TestValidateGoogleToken:
    """Tests for validate_google_token()."""

    @patch("dango.oauth.validation.requests")
    def test_valid_token(self, mock_requests: MagicMock) -> None:
        """Valid refresh token returns valid result with email."""
        # Mock token exchange
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "ya29.fresh-token"}

        # Mock userinfo
        userinfo_resp = MagicMock()
        userinfo_resp.status_code = 200
        userinfo_resp.json.return_value = {"email": "user@gmail.com", "name": "Test User"}

        mock_requests.post.return_value = token_resp
        mock_requests.get.return_value = userinfo_resp

        cred = make_google_credential()
        result = validate_google_token(cred)

        assert result.valid is True
        assert result.account_info == "user@gmail.com"
        assert result.error_code is None

    @patch("dango.oauth.validation.requests")
    def test_revoked_refresh_token(self, mock_requests: MagicMock) -> None:
        """Revoked refresh token (invalid_grant) returns revoked result."""
        token_resp = MagicMock()
        token_resp.status_code = 400
        token_resp.json.return_value = {
            "error": "invalid_grant",
            "error_description": "Token has been revoked.",
        }
        mock_requests.post.return_value = token_resp

        cred = make_google_credential()
        result = validate_google_token(cred)

        assert result.valid is False
        assert result.error_code == "revoked"
        assert "Re-authenticate" in result.message

    def test_missing_credentials(self) -> None:
        """Missing client_id/secret/refresh_token returns missing_credentials."""
        cred = make_google_credential(credentials={"client_id": "x"})
        result = validate_google_token(cred)

        assert result.valid is False
        assert result.error_code == "missing_credentials"

    @patch("dango.oauth.validation.requests")
    def test_network_error_returns_valid(self, mock_requests: MagicMock) -> None:
        """ConnectionError gives benefit of the doubt (valid=True, error_code=network_error)."""
        import requests as real_requests

        mock_requests.post.side_effect = real_requests.ConnectionError("DNS resolution failed")
        mock_requests.ConnectionError = real_requests.ConnectionError
        mock_requests.Timeout = real_requests.Timeout

        cred = make_google_credential()
        result = validate_google_token(cred)

        assert result.valid is True
        assert result.error_code == "network_error"

    @patch("dango.oauth.validation.requests")
    def test_userinfo_failure(self, mock_requests: MagicMock) -> None:
        """Token exchange succeeds but userinfo fails → revoked."""
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "ya29.fresh-token"}

        userinfo_resp = MagicMock()
        userinfo_resp.status_code = 403

        mock_requests.post.return_value = token_resp
        mock_requests.get.return_value = userinfo_resp

        cred = make_google_credential()
        result = validate_google_token(cred)

        assert result.valid is False
        assert result.error_code == "revoked"


@pytest.mark.unit
class TestValidateFacebookToken:
    """Tests for validate_facebook_token()."""

    @patch("dango.oauth.validation.requests")
    def test_valid_token(self, mock_requests: MagicMock) -> None:
        """Valid token returns valid result."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"name": "John Doe", "id": "123456789"}
        mock_requests.get.return_value = resp

        cred = make_facebook_credential()
        result = validate_facebook_token(cred)

        assert result.valid is True
        assert result.account_info == "John Doe"

    def test_expired_token(self) -> None:
        """Expired token (stored expiry in past) returns expired result."""
        cred = make_facebook_credential(
            expires_at=datetime.now() - timedelta(days=1),
        )
        result = validate_facebook_token(cred)

        assert result.valid is False
        assert result.error_code == "expired"

    @patch("dango.oauth.validation.requests")
    def test_revoked_token(self, mock_requests: MagicMock) -> None:
        """401 from /me returns revoked result."""
        resp = MagicMock()
        resp.status_code = 401
        mock_requests.get.return_value = resp

        cred = make_facebook_credential()
        result = validate_facebook_token(cred)

        assert result.valid is False
        assert result.error_code == "revoked"

    def test_missing_access_token(self) -> None:
        """Missing access_token returns missing_credentials."""
        cred = make_facebook_credential(credentials={})
        result = validate_facebook_token(cred)

        assert result.valid is False
        assert result.error_code == "missing_credentials"

    @patch("dango.oauth.validation.requests")
    def test_network_error_returns_valid(self, mock_requests: MagicMock) -> None:
        """Network error gives benefit of the doubt."""
        import requests as real_requests

        mock_requests.get.side_effect = real_requests.Timeout("Connection timed out")
        mock_requests.ConnectionError = real_requests.ConnectionError
        mock_requests.Timeout = real_requests.Timeout

        cred = make_facebook_credential()
        result = validate_facebook_token(cred)

        assert result.valid is True
        assert result.error_code == "network_error"


@pytest.mark.unit
class TestValidateShopifyToken:
    """Tests for validate_shopify_token()."""

    @patch("dango.oauth.validation.requests")
    def test_valid_token(self, mock_requests: MagicMock) -> None:
        """Valid token returns shop name."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"shop": {"name": "My Awesome Store"}}
        mock_requests.get.return_value = resp

        cred = make_shopify_credential()
        result = validate_shopify_token(cred)

        assert result.valid is True
        assert "My Awesome Store" in result.account_info

    @patch("dango.oauth.validation.requests")
    def test_invalid_token(self, mock_requests: MagicMock) -> None:
        """401 from shop.json returns revoked result."""
        resp = MagicMock()
        resp.status_code = 401
        mock_requests.get.return_value = resp

        cred = make_shopify_credential()
        result = validate_shopify_token(cred)

        assert result.valid is False
        assert result.error_code == "revoked"

    def test_missing_credentials(self) -> None:
        """Missing shop_url or password returns missing_credentials."""
        cred = make_shopify_credential(credentials={"shop_url": "x.myshopify.com"})
        result = validate_shopify_token(cred)

        assert result.valid is False
        assert result.error_code == "missing_credentials"

    @patch("dango.oauth.validation.requests")
    def test_network_error_returns_valid(self, mock_requests: MagicMock) -> None:
        """Network error gives benefit of the doubt."""
        import requests as real_requests

        mock_requests.get.side_effect = real_requests.ConnectionError("Connection refused")
        mock_requests.ConnectionError = real_requests.ConnectionError
        mock_requests.Timeout = real_requests.Timeout

        cred = make_shopify_credential()
        result = validate_shopify_token(cred)

        assert result.valid is True
        assert result.error_code == "network_error"


@pytest.mark.unit
class TestValidateToken:
    """Tests for validate_token() routing."""

    @patch("dango.oauth.validation.requests")
    def test_routes_google(self, mock_requests: MagicMock) -> None:
        """Routes google provider to validate_google_token (returns valid)."""
        # Mock successful token exchange + userinfo
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "ya29.test"}
        userinfo_resp = MagicMock()
        userinfo_resp.status_code = 200
        userinfo_resp.json.return_value = {"email": "user@gmail.com"}
        mock_requests.post.return_value = token_resp
        mock_requests.get.return_value = userinfo_resp

        cred = make_google_credential()
        result = validate_token(cred)
        assert result.valid is True
        assert result.provider == "google"

    @patch("dango.oauth.validation.requests")
    def test_routes_facebook(self, mock_requests: MagicMock) -> None:
        """Routes facebook_ads provider to validate_facebook_token."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"name": "Test", "id": "123"}
        mock_requests.get.return_value = resp

        cred = make_facebook_credential()
        result = validate_token(cred)
        assert result.valid is True
        assert result.provider == "facebook_ads"

    @patch("dango.oauth.validation.requests")
    def test_routes_shopify(self, mock_requests: MagicMock) -> None:
        """Routes shopify provider to validate_shopify_token."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"shop": {"name": "Test Shop"}}
        mock_requests.get.return_value = resp

        cred = make_shopify_credential()
        result = validate_token(cred)
        assert result.valid is True
        assert result.provider == "shopify"

    def test_unknown_provider(self) -> None:
        """Unknown provider returns valid (no validator)."""
        cred = make_oauth_credential(provider="unknown_provider")
        result = validate_token(cred)
        assert result.valid is True
        assert "No validator" in result.message


@pytest.mark.unit
class TestValidateBeforeSync:
    """Tests for validate_before_sync() pre-sync gate."""

    @patch("dango.oauth.validation.OAuthStorage")
    @patch("dango.oauth.validation.validate_token")
    def test_non_oauth_source_passes(
        self, mock_validate: MagicMock, mock_storage_cls: MagicMock, tmp_path: Path
    ) -> None:
        """Non-OAuth source (e.g. csv) passes immediately without validation."""
        validate_before_sync("csv", tmp_path)
        mock_validate.assert_not_called()

    @patch("dango.oauth.validation.OAuthStorage")
    @patch("dango.oauth.validation.validate_token")
    def test_valid_token_passes(
        self, mock_validate: MagicMock, mock_storage_cls: MagicMock, tmp_path: Path
    ) -> None:
        """Valid OAuth token passes without raising."""
        mock_storage = MagicMock()
        mock_storage.get.return_value = make_google_credential()
        mock_storage_cls.return_value = mock_storage

        mock_validate.return_value = TokenValidationResult(
            source_type="google_sheets", provider="google", valid=True, message="ok"
        )

        validate_before_sync("google_sheets", tmp_path)  # Should not raise

    @patch("dango.oauth.validation.OAuthStorage")
    @patch("dango.oauth.validation.validate_token")
    def test_revoked_token_raises(
        self, mock_validate: MagicMock, mock_storage_cls: MagicMock, tmp_path: Path
    ) -> None:
        """Revoked token raises OAuthTokenRevokedError."""
        mock_storage = MagicMock()
        mock_storage.get.return_value = make_google_credential()
        mock_storage_cls.return_value = mock_storage

        mock_validate.return_value = TokenValidationResult(
            source_type="google_sheets",
            provider="google",
            valid=False,
            message="Token revoked",
            error_code="revoked",
        )

        with pytest.raises(OAuthTokenRevokedError):
            validate_before_sync("google_sheets", tmp_path)

    @patch("dango.oauth.validation.OAuthStorage")
    @patch("dango.oauth.validation.validate_token")
    def test_expired_token_raises(
        self, mock_validate: MagicMock, mock_storage_cls: MagicMock, tmp_path: Path
    ) -> None:
        """Expired token raises OAuthTokenExpiredError."""
        mock_storage = MagicMock()
        mock_storage.get.return_value = make_facebook_credential()
        mock_storage_cls.return_value = mock_storage

        mock_validate.return_value = TokenValidationResult(
            source_type="facebook_ads",
            provider="facebook_ads",
            valid=False,
            message="Token expired",
            error_code="expired",
        )

        with pytest.raises(OAuthTokenExpiredError):
            validate_before_sync("facebook_ads", tmp_path)

    @patch("dango.oauth.validation.OAuthStorage")
    @patch("dango.oauth.validation.validate_token")
    def test_network_error_passes_silently(
        self, mock_validate: MagicMock, mock_storage_cls: MagicMock, tmp_path: Path
    ) -> None:
        """Network error does NOT raise — silent pass."""
        mock_storage = MagicMock()
        mock_storage.get.return_value = make_google_credential()
        mock_storage_cls.return_value = mock_storage

        mock_validate.return_value = TokenValidationResult(
            source_type="google_sheets",
            provider="google",
            valid=True,
            message="Network error",
            error_code="network_error",
        )

        validate_before_sync("google_sheets", tmp_path)  # Should not raise

    @patch("dango.oauth.validation.OAuthStorage")
    def test_no_stored_credential_passes(self, mock_storage_cls: MagicMock, tmp_path: Path) -> None:
        """Missing stored credential passes (let dlt handle it)."""
        mock_storage = MagicMock()
        mock_storage.get.return_value = None
        mock_storage_cls.return_value = mock_storage

        validate_before_sync("google_sheets", tmp_path)  # Should not raise


@pytest.mark.unit
class TestValidateAllTokens:
    """Tests for validate_all_tokens()."""

    @patch("dango.oauth.validation.validate_token")
    @patch("dango.oauth.validation.OAuthStorage")
    def test_validates_all_stored_tokens(
        self, mock_storage_cls: MagicMock, mock_validate: MagicMock, tmp_path: Path
    ) -> None:
        """Validates each stored credential and returns results."""
        creds = [make_google_credential(), make_facebook_credential()]
        mock_storage = MagicMock()
        mock_storage.list.return_value = creds
        mock_storage_cls.return_value = mock_storage

        mock_validate.side_effect = [
            TokenValidationResult(
                source_type="google_sheets", provider="google", valid=True, message="ok"
            ),
            TokenValidationResult(
                source_type="facebook_ads", provider="facebook_ads", valid=False, message="revoked"
            ),
        ]

        results = validate_all_tokens(tmp_path)

        assert len(results) == 2
        assert results[0].valid is True
        assert results[1].valid is False

    @patch("dango.oauth.validation.OAuthStorage")
    def test_empty_when_no_credentials(self, mock_storage_cls: MagicMock, tmp_path: Path) -> None:
        """Returns empty list when no credentials stored."""
        mock_storage = MagicMock()
        mock_storage.list.return_value = []
        mock_storage_cls.return_value = mock_storage

        results = validate_all_tokens(tmp_path)
        assert results == []
