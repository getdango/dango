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


@pytest.mark.unit
class TestSaveDoTokenProjectLevel:
    """Test project-level credential storage (P0-8 fix)."""

    def test_save_to_project_level(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """Passing project_root saves to project/.dango/credentials."""
        import dango.config.cloud_credentials as cc

        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        project = tmp_path / "myproject"
        project.mkdir()

        cc.save_do_token("proj-token", project_root=project)

        cred_file = project / ".dango" / "credentials"
        assert cred_file.is_file()
        mode = stat.S_IMODE(os.stat(cred_file).st_mode)
        assert mode == 0o600

    def test_save_project_level_does_not_touch_global(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        """Project-level save should not modify global credentials."""
        import dango.config.cloud_credentials as cc

        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setattr(cc, "_CREDENTIALS_DIR", global_dir)
        monkeypatch.setattr(cc, "_CREDENTIALS_FILE", global_dir / "credentials")
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        # Save global token first
        cc.save_do_token("global-token")

        # Save project-level token
        project = tmp_path / "project"
        project.mkdir()
        cc.save_do_token("proj-token", project_root=project)

        # Global should still have original token
        assert cc.get_do_token() == "global-token"

    def test_get_reads_project_level_first(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """get_do_token with project_root should prefer project-level token."""
        import dango.config.cloud_credentials as cc

        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setattr(cc, "_CREDENTIALS_DIR", global_dir)
        monkeypatch.setattr(cc, "_CREDENTIALS_FILE", global_dir / "credentials")
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        # Save different tokens at each level
        cc.save_do_token("global-token")
        project = tmp_path / "project"
        project.mkdir()
        cc.save_do_token("proj-token", project_root=project)

        # Project-level should win
        assert cc.get_do_token(project_root=project) == "proj-token"
        # Without project_root, global is returned
        assert cc.get_do_token() == "global-token"

    def test_two_projects_different_tokens(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """Two projects should have independent tokens."""
        import dango.config.cloud_credentials as cc

        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        proj_a = tmp_path / "project_a"
        proj_a.mkdir()
        proj_b = tmp_path / "project_b"
        proj_b.mkdir()

        cc.save_do_token("token-a", project_root=proj_a)
        cc.save_do_token("token-b", project_root=proj_b)

        assert cc.get_do_token(project_root=proj_a) == "token-a"
        assert cc.get_do_token(project_root=proj_b) == "token-b"
