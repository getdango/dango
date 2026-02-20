"""dango/auth/oauth_login.py

OAuth social login provider abstraction.

Defines a provider interface (``OAuthLoginProvider``) and concrete
implementations for Google and GitHub.  Route handlers in
``web/routes/auth.py`` use these providers to drive the authorization
code flow without knowing provider-specific details.
"""

from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from dango.config.models import OAuthProviderConfig

_HTTP_TIMEOUT: float = 10.0


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OAuthUserInfo:
    """User identity returned by an OAuth provider after code exchange."""

    provider: str
    provider_id: str
    email: str
    name: str | None


class OAuthLoginError(Exception):
    """Raised when an OAuth login flow fails (caught by route handlers)."""


# ---------------------------------------------------------------------------
# Provider ABC
# ---------------------------------------------------------------------------


class OAuthLoginProvider(ABC):
    """Base class for OAuth social login providers."""

    name: str
    display_name: str
    icon_svg: str

    def __init__(self, config: OAuthProviderConfig) -> None:
        self._client_id = config.client_id
        self._client_secret = config.client_secret

    @abstractmethod
    def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Build the provider's authorization URL for the redirect."""

    @abstractmethod
    async def exchange_code(self, code: str, redirect_uri: str) -> OAuthUserInfo:
        """Exchange an authorization code for user info."""


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------

# fmt: off
_GOOGLE_ICON_SVG = (
    '<svg viewBox="0 0 24 24" width="20" height="20">'
    '<path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z"/>'
    '<path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>'
    '<path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>'
    '<path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>'
    '</svg>'
)
# fmt: on


class GoogleOAuthProvider(OAuthLoginProvider):
    """Google OAuth 2.0 login provider."""

    name = "google"
    display_name = "Google"
    icon_svg = _GOOGLE_ICON_SVG

    _AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    _TOKEN_URL = "https://oauth2.googleapis.com/token"
    _USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

    def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Build Google authorization URL."""
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
        return f"{self._AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> OAuthUserInfo:
        """Exchange Google authorization code for user info."""
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                # Exchange code for access token
                token_resp = await client.post(
                    self._TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": redirect_uri,
                    },
                )
                if token_resp.status_code != 200:
                    raise OAuthLoginError("Failed to exchange Google authorization code")

                token_data: dict[str, Any] = token_resp.json()
                access_token = token_data.get("access_token")
                if not access_token:
                    raise OAuthLoginError("No access token in Google response")

                # Fetch user info
                user_resp = await client.get(
                    self._USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if user_resp.status_code != 200:
                    raise OAuthLoginError("Failed to fetch Google user info")

                user_data: dict[str, Any] = user_resp.json()
                email = user_data.get("email")
                if not email:
                    raise OAuthLoginError("No email in Google user info")

                return OAuthUserInfo(
                    provider="google",
                    provider_id=str(user_data.get("id", "")),
                    email=email,
                    name=user_data.get("name"),
                )
        except OAuthLoginError:
            raise
        except httpx.HTTPError as exc:
            raise OAuthLoginError("Failed to connect to Google") from exc


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

# fmt: off
_GITHUB_ICON_SVG = (
    '<svg viewBox="0 0 24 24" width="20" height="20">'
    '<path fill="currentColor" d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0 1 12 6.844a9.59 9.59 0 0 1 2.504.337c1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.02 10.02 0 0 0 22 12.017C22 6.484 17.522 2 12 2z"/>'
    '</svg>'
)
# fmt: on


class GitHubOAuthProvider(OAuthLoginProvider):
    """GitHub OAuth login provider."""

    name = "github"
    display_name = "GitHub"
    icon_svg = _GITHUB_ICON_SVG

    _AUTH_URL = "https://github.com/login/oauth/authorize"
    _TOKEN_URL = "https://github.com/login/oauth/access_token"
    _USER_URL = "https://api.github.com/user"
    _EMAILS_URL = "https://api.github.com/user/emails"

    def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Build GitHub authorization URL."""
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": "user:email",
            "state": state,
        }
        return f"{self._AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> OAuthUserInfo:
        """Exchange GitHub authorization code for user info."""
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                # Exchange code for access token
                token_resp = await client.post(
                    self._TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                )
                if token_resp.status_code != 200:
                    raise OAuthLoginError("Failed to exchange GitHub authorization code")

                token_data: dict[str, Any] = token_resp.json()
                access_token = token_data.get("access_token")
                if not access_token:
                    raise OAuthLoginError("No access token in GitHub response")

                auth_headers = {"Authorization": f"Bearer {access_token}"}

                # Fetch user profile
                user_resp = await client.get(self._USER_URL, headers=auth_headers)
                if user_resp.status_code != 200:
                    raise OAuthLoginError("Failed to fetch GitHub user info")

                user_data: dict[str, Any] = user_resp.json()
                email = user_data.get("email")

                # Fallback: fetch primary verified email from /user/emails
                if not email:
                    email = await self._fetch_primary_email(client, auth_headers)

                if not email:
                    raise OAuthLoginError(
                        "No verified email found on your GitHub account. "
                        "Please add a verified email to GitHub and try again."
                    )

                return OAuthUserInfo(
                    provider="github",
                    provider_id=str(user_data.get("id", "")),
                    email=email,
                    name=user_data.get("name") or user_data.get("login"),
                )
        except OAuthLoginError:
            raise
        except httpx.HTTPError as exc:
            raise OAuthLoginError("Failed to connect to GitHub") from exc

    async def _fetch_primary_email(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
    ) -> str | None:
        """Fetch the user's primary verified email from GitHub /user/emails."""
        resp = await client.get(self._EMAILS_URL, headers=headers)
        if resp.status_code != 200:
            return None
        emails: list[dict[str, Any]] = resp.json()
        # Find primary + verified email
        for entry in emails:
            if entry.get("primary") and entry.get("verified"):
                return entry.get("email")
        # Fallback: any verified email
        for entry in emails:
            if entry.get("verified"):
                return entry.get("email")
        return None


# ---------------------------------------------------------------------------
# Registry and helpers
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, type[OAuthLoginProvider]] = {
    "google": GoogleOAuthProvider,
    "github": GitHubOAuthProvider,
}


def get_provider(name: str, config: OAuthProviderConfig) -> OAuthLoginProvider:
    """Instantiate an OAuth provider by name.

    Raises:
        OAuthLoginError: If the provider name is not recognized.
    """
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        raise OAuthLoginError(f"Unknown OAuth provider: {name}")
    return provider_cls(config)


def get_configured_providers(
    provider_configs: dict[str, OAuthProviderConfig],
) -> list[OAuthLoginProvider]:
    """Return instantiated providers for all configured entries.

    Silently skips unknown provider names.
    """
    providers: list[OAuthLoginProvider] = []
    for name, config in provider_configs.items():
        provider_cls = _PROVIDERS.get(name)
        if provider_cls is not None:
            providers.append(provider_cls(config))
    return providers


def generate_oauth_state() -> str:
    """Generate a cryptographically random CSRF state token."""
    return secrets.token_urlsafe(32)
