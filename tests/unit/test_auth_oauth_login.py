"""tests/unit/test_auth_oauth_login.py

Tests for the OAuth social login provider abstraction in dango/auth/oauth_login.py.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dango.auth.oauth_login import (
    GitHubOAuthProvider,
    GoogleOAuthProvider,
    OAuthLoginError,
    OAuthUserInfo,
    generate_oauth_state,
    get_configured_providers,
    get_provider,
)
from dango.config.models import OAuthProviderConfig


def _make_config(
    client_id: str = "test-client-id",
    client_secret: str = "test-client-secret",
) -> OAuthProviderConfig:
    return OAuthProviderConfig(client_id=client_id, client_secret=client_secret)


def _mock_async_client(
    token_status: int = 200,
    token_data: dict[str, Any] | None = None,
    user_status: int = 200,
    user_data: dict[str, Any] | None = None,
) -> AsyncMock:
    """Build a mock httpx.AsyncClient for provider tests."""
    mock_token = MagicMock()
    mock_token.status_code = token_status
    mock_token.json.return_value = token_data or {}

    mock_user = MagicMock()
    mock_user.status_code = user_status
    mock_user.json.return_value = user_data or {}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_token
    mock_client.get.return_value = mock_user
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


@pytest.mark.unit
class TestOAuthUserInfo:
    """Tests for the OAuthUserInfo dataclass."""

    def test_creation(self) -> None:
        info = OAuthUserInfo(
            provider="google", provider_id="123", email="user@example.com", name="Test User"
        )
        assert info.provider == "google"
        assert info.provider_id == "123"
        assert info.email == "user@example.com"
        assert info.name == "Test User"

    def test_frozen(self) -> None:
        info = OAuthUserInfo(provider="google", provider_id="1", email="a@b.com", name=None)
        with pytest.raises(AttributeError):
            info.email = "new@b.com"  # type: ignore[misc]

    def test_name_optional(self) -> None:
        info = OAuthUserInfo(provider="github", provider_id="1", email="a@b.com", name=None)
        assert info.name is None


@pytest.mark.unit
class TestGenerateOAuthState:
    """Tests for generate_oauth_state()."""

    def test_uniqueness(self) -> None:
        states = {generate_oauth_state() for _ in range(50)}
        assert len(states) == 50

    def test_url_safe(self) -> None:
        state = generate_oauth_state()
        assert all(c.isalnum() or c in "-_" for c in state)

    def test_sufficient_length(self) -> None:
        state = generate_oauth_state()
        assert len(state) >= 32


@pytest.mark.unit
class TestGetProvider:
    """Tests for get_provider() factory."""

    def test_google(self) -> None:
        provider = get_provider("google", _make_config())
        assert isinstance(provider, GoogleOAuthProvider)
        assert provider.name == "google"

    def test_github(self) -> None:
        provider = get_provider("github", _make_config())
        assert isinstance(provider, GitHubOAuthProvider)
        assert provider.name == "github"

    def test_unknown_raises(self) -> None:
        with pytest.raises(OAuthLoginError, match="Unknown OAuth provider"):
            get_provider("facebook", _make_config())


@pytest.mark.unit
class TestGetConfiguredProviders:
    """Tests for get_configured_providers()."""

    def test_empty(self) -> None:
        assert get_configured_providers({}) == []

    def test_single_provider(self) -> None:
        result = get_configured_providers({"google": _make_config()})
        assert len(result) == 1
        assert result[0].name == "google"

    def test_multiple_providers(self) -> None:
        configs = {
            "google": _make_config(client_id="g-id"),
            "github": _make_config(client_id="gh-id"),
        }
        result = get_configured_providers(configs)
        assert len(result) == 2
        assert {p.name for p in result} == {"google", "github"}

    def test_unknown_provider_skipped(self) -> None:
        configs = {"google": _make_config(), "facebook": _make_config()}
        result = get_configured_providers(configs)
        assert len(result) == 1
        assert result[0].name == "google"


@pytest.mark.unit
class TestGoogleOAuthProvider:
    """Tests for GoogleOAuthProvider."""

    def test_class_attributes(self) -> None:
        provider = GoogleOAuthProvider(_make_config())
        assert provider.name == "google"
        assert provider.display_name == "Google"
        assert "<svg" in provider.icon_svg

    def test_get_authorization_url(self) -> None:
        provider = GoogleOAuthProvider(_make_config(client_id="my-client"))
        url = provider.get_authorization_url(
            redirect_uri="https://app.example.com/callback",
            state="test-state-123",
        )
        assert "accounts.google.com" in url
        assert "client_id=my-client" in url
        assert "redirect_uri=https" in url
        assert "state=test-state-123" in url
        assert "scope=openid+email+profile" in url
        assert "response_type=code" in url

    def test_exchange_code_success(self) -> None:
        provider = GoogleOAuthProvider(_make_config())
        mock_client = _mock_async_client(
            token_data={"access_token": "google-token"},
            user_data={"id": "g-12345", "email": "user@gmail.com", "name": "Google User"},
        )
        with patch("dango.auth.oauth_login.httpx.AsyncClient", return_value=mock_client):
            info = asyncio.run(
                provider.exchange_code("auth-code", "https://app.example.com/callback")
            )
        assert info.provider == "google"
        assert info.provider_id == "g-12345"
        assert info.email == "user@gmail.com"
        assert info.name == "Google User"

    def test_exchange_code_token_failure(self) -> None:
        provider = GoogleOAuthProvider(_make_config())
        mock_client = _mock_async_client(token_status=400)
        with (
            patch("dango.auth.oauth_login.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(OAuthLoginError, match="Failed to exchange Google"),
        ):
            asyncio.run(provider.exchange_code("bad-code", "https://app.example.com/callback"))

    def test_exchange_code_no_access_token(self) -> None:
        provider = GoogleOAuthProvider(_make_config())
        mock_client = _mock_async_client(token_data={"error": "invalid_grant"})
        with (
            patch("dango.auth.oauth_login.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(OAuthLoginError, match="No access token"),
        ):
            asyncio.run(provider.exchange_code("code", "https://app.example.com/callback"))

    def test_exchange_code_no_email(self) -> None:
        provider = GoogleOAuthProvider(_make_config())
        mock_client = _mock_async_client(
            token_data={"access_token": "token"},
            user_data={"id": "123", "name": "No Email"},
        )
        with (
            patch("dango.auth.oauth_login.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(OAuthLoginError, match="No email"),
        ):
            asyncio.run(provider.exchange_code("code", "https://app.example.com/callback"))

    def test_exchange_code_user_info_failure(self) -> None:
        provider = GoogleOAuthProvider(_make_config())
        mock_client = _mock_async_client(token_data={"access_token": "token"}, user_status=403)
        with (
            patch("dango.auth.oauth_login.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(OAuthLoginError, match="Failed to fetch Google user info"),
        ):
            asyncio.run(provider.exchange_code("code", "https://app.example.com/callback"))


@pytest.mark.unit
class TestGitHubOAuthProvider:
    """Tests for GitHubOAuthProvider."""

    def test_class_attributes(self) -> None:
        provider = GitHubOAuthProvider(_make_config())
        assert provider.name == "github"
        assert provider.display_name == "GitHub"
        assert "<svg" in provider.icon_svg

    def test_get_authorization_url(self) -> None:
        provider = GitHubOAuthProvider(_make_config(client_id="gh-client"))
        url = provider.get_authorization_url(
            redirect_uri="https://app.example.com/callback", state="test-state"
        )
        assert "github.com/login/oauth/authorize" in url
        assert "client_id=gh-client" in url
        assert "state=test-state" in url
        assert "scope=user" in url

    def test_exchange_code_public_email(self) -> None:
        """GitHub user with public email — no /user/emails fallback needed."""
        provider = GitHubOAuthProvider(_make_config())
        mock_client = _mock_async_client(
            token_data={"access_token": "gh-token"},
            user_data={"id": 42, "email": "dev@github.com", "name": "Dev User", "login": "dev"},
        )
        with patch("dango.auth.oauth_login.httpx.AsyncClient", return_value=mock_client):
            info = asyncio.run(provider.exchange_code("code", "https://app.example.com/callback"))
        assert info.provider == "github"
        assert info.provider_id == "42"
        assert info.email == "dev@github.com"
        assert info.name == "Dev User"

    def test_exchange_code_private_email_fallback(self) -> None:
        """GitHub user with private email — fetches from /user/emails."""
        provider = GitHubOAuthProvider(_make_config())

        mock_token = MagicMock()
        mock_token.status_code = 200
        mock_token.json.return_value = {"access_token": "gh-token"}

        mock_user = MagicMock()
        mock_user.status_code = 200
        mock_user.json.return_value = {
            "id": 99,
            "email": None,
            "name": None,
            "login": "privateuser",
        }

        mock_emails = MagicMock()
        mock_emails.status_code = 200
        mock_emails.json.return_value = [
            {"email": "noreply@github.com", "primary": False, "verified": True},
            {"email": "private@example.com", "primary": True, "verified": True},
        ]

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            if "user/emails" in url:
                return mock_emails
            return mock_user

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_token
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("dango.auth.oauth_login.httpx.AsyncClient", return_value=mock_client):
            info = asyncio.run(provider.exchange_code("code", "https://app.example.com/callback"))
        assert info.email == "private@example.com"
        assert info.name == "privateuser"

    def test_exchange_code_no_verified_email(self) -> None:
        """GitHub user with no verified email raises OAuthLoginError."""
        provider = GitHubOAuthProvider(_make_config())

        mock_token = MagicMock()
        mock_token.status_code = 200
        mock_token.json.return_value = {"access_token": "token"}

        mock_user = MagicMock()
        mock_user.status_code = 200
        mock_user.json.return_value = {"id": 1, "email": None, "login": "nomail"}

        mock_emails = MagicMock()
        mock_emails.status_code = 200
        mock_emails.json.return_value = [
            {"email": "unverified@example.com", "primary": True, "verified": False},
        ]

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            if "user/emails" in url:
                return mock_emails
            return mock_user

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_token
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("dango.auth.oauth_login.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(OAuthLoginError, match="No verified email"),
        ):
            asyncio.run(provider.exchange_code("code", "https://app.example.com/callback"))

    def test_exchange_code_token_failure(self) -> None:
        provider = GitHubOAuthProvider(_make_config())
        mock_client = _mock_async_client(token_status=401)
        with (
            patch("dango.auth.oauth_login.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(OAuthLoginError, match="Failed to exchange GitHub"),
        ):
            asyncio.run(provider.exchange_code("bad", "https://app.example.com/callback"))
