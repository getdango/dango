"""tests/unit/test_metabase_bridge.py

Tests for async Metabase session bridging in dango/auth/metabase_bridge.py.
All Metabase HTTP calls are mocked via ``unittest.mock.patch``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from dango.auth.metabase_bridge import (
    _load_metabase_url,
    bridge_metabase_login,
    bridge_metabase_logout,
    ensure_metabase_synced,
    get_metabase_url,
)
from dango.auth.models import Role, User

# Lazy imports in metabase_bridge.py mean we must patch at the origin module.
_DECRYPT_TARGET = "dango.auth.metabase_sync.decrypt_metabase_password"
_SYNC_TARGET = "dango.auth.metabase_sync.sync_user_to_metabase"
_HTTPX_CLIENT = "dango.auth.metabase_bridge.httpx.AsyncClient"


def _mb_yml(tmp_path: Path, url: str = "http://metabase:3000") -> Path:
    """Write a minimal metabase.yml and return the project root."""
    d = tmp_path / ".dango"
    d.mkdir(exist_ok=True)
    (d / "metabase.yml").write_text(
        yaml.safe_dump({"metabase_url": url, "admin": {"email": "a@b.c", "password": "pw"}})
    )
    return tmp_path


def _make_user(**kw: Any) -> User:
    """Build a User model with Metabase fields populated."""
    defaults: dict[str, Any] = {
        "email": "test@example.com",
        "password_hash": "$2b$12$fake",
        "role": Role.EDITOR,
        "metabase_user_id": 42,
        "metabase_password_enc": "encrypted_blob",
    }
    defaults.update(kw)
    return User(**defaults)


def _mock_httpx_client(
    method: str, response: MagicMock | None = None, side_effect: Exception | None = None
) -> AsyncMock:
    """Create a mock httpx.AsyncClient context manager."""
    mock_client = AsyncMock()
    if side_effect is not None:
        setattr(mock_client, method, AsyncMock(side_effect=side_effect))
    else:
        setattr(mock_client, method, AsyncMock(return_value=response))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _resp(status: int = 200, data: dict[str, Any] | None = None) -> MagicMock:
    """Build a mock httpx response."""
    r = MagicMock()
    r.status_code = status
    r.json.return_value = data if data is not None else {}
    return r


# ---------------------------------------------------------------------------
# _load_metabase_url / get_metabase_url
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadMetabaseUrl:
    """Tests for _load_metabase_url (sync) and get_metabase_url (async)."""

    def test_file_exists(self, tmp_path: Path) -> None:
        root = _mb_yml(tmp_path, "http://metabase:3000")
        assert _load_metabase_url(root) == "http://metabase:3000"

    def test_file_missing(self, tmp_path: Path) -> None:
        assert _load_metabase_url(tmp_path) is None

    def test_missing_key(self, tmp_path: Path) -> None:
        d = tmp_path / ".dango"
        d.mkdir()
        (d / "metabase.yml").write_text(yaml.safe_dump({"admin": {}}))
        assert _load_metabase_url(tmp_path) is None

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        d = tmp_path / ".dango"
        d.mkdir()
        (d / "metabase.yml").write_text("{{invalid")
        assert _load_metabase_url(tmp_path) is None

    def test_async_wrapper(self, tmp_path: Path) -> None:
        root = _mb_yml(tmp_path)
        result = asyncio.run(get_metabase_url(root))
        assert result == "http://metabase:3000"


# ---------------------------------------------------------------------------
# bridge_metabase_login
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBridgeMetabaseLogin:
    """Tests for bridge_metabase_login."""

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def test_success(self, tmp_path: Path) -> None:
        root = _mb_yml(tmp_path)
        user = _make_user()
        mock_client = _mock_httpx_client("post", _resp(200, {"id": "sess-abc-123"}))

        with (
            patch(_DECRYPT_TARGET, return_value="mb_pw"),
            patch(_HTTPX_CLIENT, return_value=mock_client),
        ):
            result = self._run(bridge_metabase_login(user, root))
            assert result == "sess-abc-123"
            mock_client.post.assert_called_once()

    def test_no_credentials(self, tmp_path: Path) -> None:
        root = _mb_yml(tmp_path)
        user = _make_user(metabase_password_enc=None)
        result = self._run(bridge_metabase_login(user, root))
        assert result is None

    def test_no_metabase_user_id(self, tmp_path: Path) -> None:
        root = _mb_yml(tmp_path)
        user = _make_user(metabase_user_id=None)
        result = self._run(bridge_metabase_login(user, root))
        assert result is None

    def test_no_url(self, tmp_path: Path) -> None:
        user = _make_user()
        result = self._run(bridge_metabase_login(user, tmp_path))
        assert result is None

    def test_metabase_401(self, tmp_path: Path) -> None:
        root = _mb_yml(tmp_path)
        user = _make_user()
        mock_client = _mock_httpx_client("post", _resp(401))

        with (
            patch(_DECRYPT_TARGET, return_value="pw"),
            patch(_HTTPX_CLIENT, return_value=mock_client),
        ):
            result = self._run(bridge_metabase_login(user, root))
            assert result is None

    def test_network_error(self, tmp_path: Path) -> None:
        root = _mb_yml(tmp_path)
        user = _make_user()
        mock_client = _mock_httpx_client("post", side_effect=ConnectionError("refused"))

        with (
            patch(_DECRYPT_TARGET, return_value="pw"),
            patch(_HTTPX_CLIENT, return_value=mock_client),
        ):
            result = self._run(bridge_metabase_login(user, root))
            assert result is None

    def test_decrypt_error(self, tmp_path: Path) -> None:
        root = _mb_yml(tmp_path)
        user = _make_user()

        with patch(_DECRYPT_TARGET, side_effect=ValueError("bad key")):
            result = self._run(bridge_metabase_login(user, root))
            assert result is None


# ---------------------------------------------------------------------------
# bridge_metabase_logout
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBridgeMetabaseLogout:
    """Tests for bridge_metabase_logout."""

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def test_success_204(self, tmp_path: Path) -> None:
        root = _mb_yml(tmp_path)
        mock_client = _mock_httpx_client("delete", _resp(204))

        with patch(_HTTPX_CLIENT, return_value=mock_client):
            result = self._run(bridge_metabase_logout("sess-123", root))
            assert result is True

    def test_success_200(self, tmp_path: Path) -> None:
        root = _mb_yml(tmp_path)
        mock_client = _mock_httpx_client("delete", _resp(200))

        with patch(_HTTPX_CLIENT, return_value=mock_client):
            result = self._run(bridge_metabase_logout("sess-123", root))
            assert result is True

    def test_failure_500(self, tmp_path: Path) -> None:
        root = _mb_yml(tmp_path)
        mock_client = _mock_httpx_client("delete", _resp(500))

        with patch(_HTTPX_CLIENT, return_value=mock_client):
            result = self._run(bridge_metabase_logout("sess-123", root))
            assert result is False

    def test_no_url(self, tmp_path: Path) -> None:
        result = self._run(bridge_metabase_logout("sess-123", tmp_path))
        assert result is False

    def test_network_error(self, tmp_path: Path) -> None:
        root = _mb_yml(tmp_path)
        mock_client = _mock_httpx_client("delete", side_effect=ConnectionError("refused"))

        with patch(_HTTPX_CLIENT, return_value=mock_client):
            result = self._run(bridge_metabase_logout("sess-123", root))
            assert result is False


# ---------------------------------------------------------------------------
# ensure_metabase_synced
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnsureMetabaseSynced:
    """Tests for ensure_metabase_synced."""

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def test_calls_sync(self, tmp_path: Path) -> None:
        with patch(_SYNC_TARGET) as mock_sync:
            self._run(
                ensure_metabase_synced(tmp_path / "auth.db", "user-1", tmp_path, "http://mb:3000")
            )
            mock_sync.assert_called_once_with(
                tmp_path / "auth.db", "user-1", tmp_path, "http://mb:3000"
            )

    def test_exception_swallowed(self, tmp_path: Path) -> None:
        with patch(_SYNC_TARGET, side_effect=RuntimeError("boom")):
            # Should not raise
            self._run(
                ensure_metabase_synced(tmp_path / "auth.db", "user-1", tmp_path, "http://mb:3000")
            )


# ---------------------------------------------------------------------------
# bridge_metabase_login — lazy sync fallback
# ---------------------------------------------------------------------------

_GET_USER_TARGET = "dango.auth.database.get_user_by_id"


@pytest.mark.unit
class TestBridgeLazySync:
    """Tests for lazy sync fallback when user has no Metabase credentials."""

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def test_lazy_sync_on_missing_credentials(self, tmp_path: Path) -> None:
        """When db_path provided and user has no credentials, lazy sync + bridge."""
        root = _mb_yml(tmp_path)
        user_no_creds = _make_user(metabase_password_enc=None, metabase_user_id=None)
        user_with_creds = _make_user()  # has metabase_password_enc + metabase_user_id
        db_path = root / ".dango" / "auth.db"

        mock_client = _mock_httpx_client("post", _resp(200, {"id": "lazy-sess-123"}))

        with (
            patch(_SYNC_TARGET, return_value=42),
            patch(_GET_USER_TARGET, return_value=user_with_creds),
            patch(_DECRYPT_TARGET, return_value="mb_pw"),
            patch(_HTTPX_CLIENT, return_value=mock_client),
        ):
            result = self._run(bridge_metabase_login(user_no_creds, root, db_path=db_path))
            assert result == "lazy-sess-123"

    def test_lazy_sync_failure(self, tmp_path: Path) -> None:
        """When lazy sync returns None, bridge returns None."""
        root = _mb_yml(tmp_path)
        user = _make_user(metabase_password_enc=None, metabase_user_id=None)
        db_path = root / ".dango" / "auth.db"

        with patch(_SYNC_TARGET, return_value=None):
            result = self._run(bridge_metabase_login(user, root, db_path=db_path))
            assert result is None

    def test_lazy_sync_user_reload_fails(self, tmp_path: Path) -> None:
        """When sync succeeds but user reload returns None, bridge returns None."""
        root = _mb_yml(tmp_path)
        user = _make_user(metabase_password_enc=None, metabase_user_id=None)
        db_path = root / ".dango" / "auth.db"

        with (
            patch(_SYNC_TARGET, return_value=42),
            patch(_GET_USER_TARGET, return_value=None),
        ):
            result = self._run(bridge_metabase_login(user, root, db_path=db_path))
            assert result is None
