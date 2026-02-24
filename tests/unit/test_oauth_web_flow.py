"""tests/unit/test_oauth_web_flow.py

Tests for dango.oauth.web_flow — OAuth token exchange helpers for the web flow.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dango.oauth.web_flow import (
    SUPPORTED_OAUTH_SOURCES,
    OAuthFlowError,
    build_facebook_auth_url,
    build_google_auth_url,
    exchange_facebook_code,
    exchange_google_code,
    fetch_google_user_info,
)


@pytest.mark.unit
class TestSupportedOAuthSources:
    """Verify the SUPPORTED_OAUTH_SOURCES mapping."""

    def test_google_sources_present(self):
        assert "google_ads" in SUPPORTED_OAUTH_SOURCES
        assert "google_analytics" in SUPPORTED_OAUTH_SOURCES
        assert "google_sheets" in SUPPORTED_OAUTH_SOURCES

    def test_facebook_source_present(self):
        assert "facebook_ads" in SUPPORTED_OAUTH_SOURCES

    def test_provider_values(self):
        assert SUPPORTED_OAUTH_SOURCES["google_ads"] == "google"
        assert SUPPORTED_OAUTH_SOURCES["facebook_ads"] == "facebook"


@pytest.mark.unit
class TestBuildGoogleAuthUrl:
    """Tests for build_google_auth_url."""

    def test_basic_url_structure(self):
        url = build_google_auth_url(
            client_id="test-client-id",
            redirect_uri="https://example.com/oauth/callback/google_ads",
            source_type="google_ads",
            state="random-state-token",
        )
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=test-client-id" in url
        assert "state=random-state-token" in url
        assert "response_type=code" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url

    def test_redirect_uri_encoded(self):
        url = build_google_auth_url(
            client_id="cid",
            redirect_uri="https://example.com/oauth/callback/google_ads",
            source_type="google_ads",
            state="s",
        )
        assert "redirect_uri=https" in url

    def test_google_ads_scopes(self):
        url = build_google_auth_url(
            client_id="cid",
            redirect_uri="https://example.com/cb",
            source_type="google_ads",
            state="s",
        )
        assert "userinfo.email" in url
        assert "adwords" in url

    def test_google_analytics_scopes(self):
        url = build_google_auth_url(
            client_id="cid",
            redirect_uri="https://example.com/cb",
            source_type="google_analytics",
            state="s",
        )
        assert "analytics.readonly" in url

    def test_google_sheets_scopes(self):
        url = build_google_auth_url(
            client_id="cid",
            redirect_uri="https://example.com/cb",
            source_type="google_sheets",
            state="s",
        )
        assert "spreadsheets.readonly" in url

    def test_unknown_source_uses_base_scopes(self):
        url = build_google_auth_url(
            client_id="cid",
            redirect_uri="https://example.com/cb",
            source_type="unknown_source",
            state="s",
        )
        assert "userinfo.email" in url


@pytest.mark.unit
class TestExchangeGoogleCode:
    """Tests for exchange_google_code."""

    @patch("dango.oauth.web_flow.requests.post")
    def test_successful_exchange(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "ya29.test",
            "refresh_token": "1//test",
            "expires_in": 3600,
        }
        mock_post.return_value = mock_resp

        result = exchange_google_code(
            code="auth-code",
            client_id="cid",
            client_secret="csecret",
            redirect_uri="https://example.com/cb",
        )
        assert result["access_token"] == "ya29.test"
        assert result["refresh_token"] == "1//test"
        mock_post.assert_called_once()

    @patch("dango.oauth.web_flow.requests.post")
    def test_failed_exchange_raises(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_post.return_value = mock_resp

        with pytest.raises(OAuthFlowError, match="Google token exchange failed"):
            exchange_google_code("code", "cid", "cs", "https://example.com/cb")

    @patch("dango.oauth.web_flow.requests.post")
    def test_network_error_raises(self, mock_post):
        import requests

        mock_post.side_effect = requests.ConnectionError("refused")

        with pytest.raises(OAuthFlowError, match="Failed to contact Google"):
            exchange_google_code("code", "cid", "cs", "https://example.com/cb")

    @patch("dango.oauth.web_flow.requests.post")
    def test_error_has_provider_attribute(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "err"
        mock_post.return_value = mock_resp

        with pytest.raises(OAuthFlowError) as exc_info:
            exchange_google_code("code", "cid", "cs", "https://example.com/cb")
        assert exc_info.value.provider == "google"


@pytest.mark.unit
class TestFetchGoogleUserInfo:
    """Tests for fetch_google_user_info."""

    @patch("dango.oauth.web_flow.requests.get")
    def test_successful_fetch(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "email": "user@example.com",
            "name": "Test User",
        }
        mock_get.return_value = mock_resp

        result = fetch_google_user_info("ya29.test")
        assert result["email"] == "user@example.com"
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        assert "Bearer ya29.test" in str(call_kwargs)

    @patch("dango.oauth.web_flow.requests.get")
    def test_failed_fetch_raises(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_get.return_value = mock_resp

        with pytest.raises(OAuthFlowError, match="Failed to fetch Google user info"):
            fetch_google_user_info("bad-token")

    @patch("dango.oauth.web_flow.requests.get")
    def test_network_error_raises(self, mock_get):
        import requests

        mock_get.side_effect = requests.ConnectionError("refused")

        with pytest.raises(OAuthFlowError, match="Failed to fetch Google user info"):
            fetch_google_user_info("token")


@pytest.mark.unit
class TestBuildFacebookAuthUrl:
    """Tests for build_facebook_auth_url."""

    def test_basic_url_structure(self):
        url = build_facebook_auth_url(
            client_id="fb-app-id",
            redirect_uri="https://example.com/oauth/callback/facebook_ads",
            state="random-state",
        )
        assert url.startswith("https://www.facebook.com/v18.0/dialog/oauth?")
        assert "client_id=fb-app-id" in url
        assert "state=random-state" in url
        assert "response_type=code" in url

    def test_scopes_included(self):
        url = build_facebook_auth_url(
            client_id="fid",
            redirect_uri="https://example.com/cb",
            state="s",
        )
        assert "ads_read" in url
        assert "ads_management" in url


@pytest.mark.unit
class TestExchangeFacebookCode:
    """Tests for exchange_facebook_code."""

    @patch("dango.oauth.web_flow.requests.get")
    def test_successful_exchange(self, mock_get):
        # First call: short-lived token
        short_resp = MagicMock()
        short_resp.status_code = 200
        short_resp.json.return_value = {"access_token": "short-token"}

        # Second call: long-lived token
        long_resp = MagicMock()
        long_resp.status_code = 200
        long_resp.json.return_value = {
            "access_token": "long-lived-token",
            "token_type": "bearer",
            "expires_in": 5184000,
        }

        mock_get.side_effect = [short_resp, long_resp]

        result = exchange_facebook_code("code", "fid", "fsecret", "https://example.com/cb")
        assert result["access_token"] == "long-lived-token"
        assert mock_get.call_count == 2

    @patch("dango.oauth.web_flow.requests.get")
    def test_short_lived_exchange_fails(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad request"
        mock_get.return_value = mock_resp

        with pytest.raises(OAuthFlowError, match="Facebook token exchange failed"):
            exchange_facebook_code("code", "fid", "fsecret", "https://example.com/cb")

    @patch("dango.oauth.web_flow.requests.get")
    def test_missing_access_token_raises(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_get.return_value = mock_resp

        with pytest.raises(OAuthFlowError, match="did not return an access token"):
            exchange_facebook_code("code", "fid", "fsecret", "https://example.com/cb")

    @patch("dango.oauth.web_flow.requests.get")
    def test_long_lived_exchange_fails(self, mock_get):
        short_resp = MagicMock()
        short_resp.status_code = 200
        short_resp.json.return_value = {"access_token": "short-token"}

        long_resp = MagicMock()
        long_resp.status_code = 400
        long_resp.text = "Error"

        mock_get.side_effect = [short_resp, long_resp]

        with pytest.raises(OAuthFlowError, match="long-lived Facebook token"):
            exchange_facebook_code("code", "fid", "fsecret", "https://example.com/cb")

    @patch("dango.oauth.web_flow.requests.get")
    def test_network_error_raises(self, mock_get):
        import requests

        mock_get.side_effect = requests.ConnectionError("refused")

        with pytest.raises(OAuthFlowError, match="Failed to contact Facebook"):
            exchange_facebook_code("code", "fid", "fsecret", "https://example.com/cb")

    @patch("dango.oauth.web_flow.requests.get")
    def test_error_has_provider_attribute(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "err"
        mock_get.return_value = mock_resp

        with pytest.raises(OAuthFlowError) as exc_info:
            exchange_facebook_code("code", "fid", "fs", "https://example.com/cb")
        assert exc_info.value.provider == "facebook"
