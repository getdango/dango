"""tests/unit/test_cloud_credentials.py

Unit tests for dango/config/cloud_credentials.py — persistent storage
for cloud provider credentials (BUG-127).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch


@pytest.mark.unit
class TestGetDoToken:
    def test_env_var_takes_priority(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """Environment variable is preferred over stored credential."""
        import dango.config.cloud_credentials as cc

        monkeypatch.setattr(cc, "_CREDENTIALS_DIR", tmp_path)
        monkeypatch.setattr(cc, "_CREDENTIALS_FILE", tmp_path / "credentials")
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "env-token")

        # Store a different token
        cc.save_do_token("stored-token")

        assert cc.get_do_token() == "env-token"

    def test_stored_token_returned(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """Stored token returned when env var is not set."""
        import dango.config.cloud_credentials as cc

        monkeypatch.setattr(cc, "_CREDENTIALS_DIR", tmp_path)
        monkeypatch.setattr(cc, "_CREDENTIALS_FILE", tmp_path / "credentials")
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        cc.save_do_token("stored-token")
        assert cc.get_do_token() == "stored-token"

    def test_none_when_nothing_stored(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """Returns None when no env var and no stored credential."""
        import dango.config.cloud_credentials as cc

        monkeypatch.setattr(cc, "_CREDENTIALS_DIR", tmp_path)
        monkeypatch.setattr(cc, "_CREDENTIALS_FILE", tmp_path / "credentials")
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        assert cc.get_do_token() is None


@pytest.mark.unit
class TestSaveDoToken:
    def test_creates_file_with_secure_permissions(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        """Credentials file is created with 0o600 permissions."""
        import dango.config.cloud_credentials as cc

        creds_file = tmp_path / "credentials"
        monkeypatch.setattr(cc, "_CREDENTIALS_DIR", tmp_path)
        monkeypatch.setattr(cc, "_CREDENTIALS_FILE", creds_file)

        cc.save_do_token("test-token")

        assert creds_file.is_file()
        mode = stat.S_IMODE(os.stat(creds_file).st_mode)
        assert mode == 0o600, f"Expected 0o600, got 0o{mode:03o}"

    def test_roundtrip(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """Token survives save + load cycle."""
        import dango.config.cloud_credentials as cc

        monkeypatch.setattr(cc, "_CREDENTIALS_DIR", tmp_path)
        monkeypatch.setattr(cc, "_CREDENTIALS_FILE", tmp_path / "credentials")
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        cc.save_do_token("roundtrip-token")
        assert cc.get_do_token() == "roundtrip-token"

    def test_overwrites_existing(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """Saving again overwrites the previous token."""
        import dango.config.cloud_credentials as cc

        monkeypatch.setattr(cc, "_CREDENTIALS_DIR", tmp_path)
        monkeypatch.setattr(cc, "_CREDENTIALS_FILE", tmp_path / "credentials")
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        cc.save_do_token("token-1")
        cc.save_do_token("token-2")
        assert cc.get_do_token() == "token-2"


@pytest.mark.unit
class TestClearDoToken:
    def test_clears_stored_token(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """Clearing removes the stored token."""
        import dango.config.cloud_credentials as cc

        monkeypatch.setattr(cc, "_CREDENTIALS_DIR", tmp_path)
        monkeypatch.setattr(cc, "_CREDENTIALS_FILE", tmp_path / "credentials")
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        cc.save_do_token("to-clear")
        assert cc.clear_do_token() is True
        assert cc.get_do_token() is None

    def test_returns_false_when_nothing_stored(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns False when no token was stored."""
        import dango.config.cloud_credentials as cc

        monkeypatch.setattr(cc, "_CREDENTIALS_DIR", tmp_path)
        monkeypatch.setattr(cc, "_CREDENTIALS_FILE", tmp_path / "credentials")

        assert cc.clear_do_token() is False

    def test_creates_parent_dir(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """Save creates ~/.dango/ directory if it doesn't exist."""
        import dango.config.cloud_credentials as cc

        nested = tmp_path / "sub" / ".dango"
        monkeypatch.setattr(cc, "_CREDENTIALS_DIR", nested)
        monkeypatch.setattr(cc, "_CREDENTIALS_FILE", nested / "credentials")

        cc.save_do_token("nested-token")
        assert nested.is_dir()
        assert (nested / "credentials").is_file()
