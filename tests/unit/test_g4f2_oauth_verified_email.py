"""tests/unit/test_g4f2_oauth_verified_email.py

OAuth login accepts only a verified email and a stable subject; the account is never looked
up or linked from an unverified identity.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dango.auth.oauth_login import GitHubOAuthProvider, GoogleOAuthProvider, OAuthLoginError
from dango.config.models import OAuthProviderConfig

_REDIRECT = "https://app.example.com/callback"


def _config() -> OAuthProviderConfig:
    return OAuthProviderConfig(client_id="cid", client_secret="csecret")


def _response(status: int, payload: Any) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def _client(routes: dict[str, MagicMock], token: MagicMock | None = None) -> AsyncMock:
    """Fake httpx client: GET answered by URL substring, POST returns the token response."""

    async def get(url: str, **kwargs: Any) -> MagicMock:
        for needle, resp in routes.items():
            if needle in url:
                return resp
        raise AssertionError(f"unexpected GET {url}")

    client = AsyncMock()
    client.post.return_value = token or _response(200, {"access_token": "tok"})
    client.get = get
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _google(userinfo: dict[str, Any]) -> Any:
    client = _client({"userinfo": _response(200, userinfo)})
    with patch("dango.auth.oauth_login.httpx.AsyncClient", return_value=client):
        return asyncio.run(GoogleOAuthProvider(_config()).exchange_code("code", _REDIRECT))


def _github(profile: dict[str, Any], emails: list[dict[str, Any]] | None) -> Any:
    routes = {"api.github.com/user/emails": _response(200, emails or [])}
    routes["api.github.com/user"] = _response(200, profile)
    # The longer URL must be matched first: dict order puts /user/emails before /user.
    client = _client(routes)
    with patch("dango.auth.oauth_login.httpx.AsyncClient", return_value=client):
        return asyncio.run(GitHubOAuthProvider(_config()).exchange_code("code", _REDIRECT))


@pytest.mark.unit
class TestGoogleVerifiedEmail:
    def test_unverified_email_is_rejected(self) -> None:
        with pytest.raises(OAuthLoginError):
            _google({"id": "1", "email": "victim@example.com", "verified_email": False})

    def test_missing_verification_flag_is_rejected(self) -> None:
        with pytest.raises(OAuthLoginError):
            _google({"id": "1", "email": "victim@example.com"})

    def test_string_flag_is_not_accepted_as_verified(self) -> None:
        with pytest.raises(OAuthLoginError):
            _google({"id": "1", "email": "victim@example.com", "verified_email": "false"})

    @pytest.mark.parametrize("subject", ["", None])
    def test_missing_subject_is_rejected(self, subject: Any) -> None:
        with pytest.raises(OAuthLoginError):
            _google({"id": subject, "email": "a@example.com", "verified_email": True})

    def test_verified_email_with_subject_is_accepted(self) -> None:
        info = _google({"id": "g-1", "email": "a@example.com", "verified_email": True})
        assert (info.provider_id, info.email) == ("g-1", "a@example.com")


@pytest.mark.unit
class TestGitHubVerifiedEmail:
    def test_profile_email_not_in_verified_entries_is_rejected(self) -> None:
        emails = [{"email": "victim@example.com", "primary": True, "verified": False}]
        with pytest.raises(OAuthLoginError):
            _github({"id": 7, "email": "victim@example.com", "login": "x"}, emails)

    def test_profile_email_is_ignored_in_favour_of_the_verified_entry(self) -> None:
        emails = [
            {"email": "victim@example.com", "primary": False, "verified": False},
            {"email": "mine@example.com", "primary": True, "verified": True},
        ]
        info = _github({"id": 7, "email": "victim@example.com", "login": "x"}, emails)
        assert info.email == "mine@example.com"

    def test_missing_subject_is_rejected(self) -> None:
        emails = [{"email": "mine@example.com", "primary": True, "verified": True}]
        with pytest.raises(OAuthLoginError):
            _github({"id": None, "email": None, "login": "x"}, emails)

    def test_verified_primary_email_is_accepted(self) -> None:
        emails = [{"email": "mine@example.com", "primary": True, "verified": True}]
        info = _github({"id": 7, "email": None, "login": "x"}, emails)
        assert (info.provider_id, info.email) == ("7", "mine@example.com")
