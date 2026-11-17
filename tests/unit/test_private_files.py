"""tests/unit/test_private_files.py

Private-file mode helper and every call site that used os.fchmod, run as on a platform without it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

import dango.platform.common.metabase_credential_migration as migration
from dango.notebooks.access_token import create_token_file, open_private_log
from dango.notebooks.safe_files import copy_regular_file
from dango.security import metabase_credentials as credentials
from dango.security.metabase_config import write_metabase_metadata
from dango.security.metabase_credentials import MetabaseCredentialStore
from dango.web.routes.secrets import _write_env_file


@pytest.fixture
def no_posix_fd_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the POSIX-only os attributes Windows lacks."""
    for name in ("fchmod", "O_NOFOLLOW", "O_NONBLOCK"):
        monkeypatch.delattr(os, name, raising=False)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.unit
class TestApplyPrivateMode:
    def test_noop_without_fchmod(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from dango.utils.private_files import apply_private_mode

        path = tmp_path / "f"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            before = _mode(path)
            monkeypatch.delattr(os, "fchmod", raising=False)
            apply_private_mode(fd, 0o600)
            assert _mode(path) == before
        finally:
            os.close(fd)

    def test_applies_mode_when_platform_has_fchmod(self, tmp_path: Path) -> None:
        from dango.utils.private_files import apply_private_mode

        path = tmp_path / "f"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            before = _mode(path)
            apply_private_mode(fd, 0o600)
            expected = 0o600 if hasattr(os, "fchmod") else before
            assert _mode(path) == expected
        finally:
            os.close(fd)

    def test_real_oserror_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dango.utils.private_files import apply_private_mode

        def _boom(fd: int, mode: int) -> None:
            raise PermissionError("denied")

        monkeypatch.setattr(os, "fchmod", _boom, raising=False)
        with pytest.raises(PermissionError):
            apply_private_mode(0, 0o600)


@pytest.mark.unit
@pytest.mark.usefixtures("no_posix_fd_api")
class TestCallSitesWithoutFchmod:
    def test_token_file_written(self, tmp_path: Path) -> None:
        path = create_token_file(tmp_path)
        assert path.read_text(encoding="utf-8")

    def test_private_log_opened(self, tmp_path: Path) -> None:
        with open_private_log(tmp_path / "marimo.log") as handle:
            handle.write("hello")
        assert (tmp_path / "marimo.log").read_text(encoding="utf-8") == "hello"

    def test_safe_copy_works(self, tmp_path: Path) -> None:
        src = tmp_path / "src.py"
        src.write_text("print(1)\n", encoding="utf-8")
        dst = tmp_path / "dst.py"
        copy_regular_file(src, dst, mode=0o644)
        assert dst.read_text(encoding="utf-8") == "print(1)\n"

    def test_migration_state_written(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / ".dango").mkdir(parents=True)
        (project / ".dango" / "project.yml").write_text(
            yaml.safe_dump({"project": {"name": "T"}}), encoding="utf-8"
        )
        result = migration.prepare_metabase_credential_migration(project)
        assert result == {"version": 1, "status": "prepared"}
        state = project / ".dango" / "state" / "metabase_credential_migration.json"
        assert state.is_file()

    def test_metabase_metadata_written(self, tmp_path: Path) -> None:
        (tmp_path / ".dango").mkdir()
        write_metabase_metadata(tmp_path, {"metabase_url": "http://localhost:3000"})
        loaded = yaml.safe_load((tmp_path / ".dango" / "metabase.yml").read_text("utf-8"))
        assert loaded == {"metabase_url": "http://localhost:3000"}

    def test_credential_fallback_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            credentials, "_LOCAL_SECRETS_DIR", tmp_path / "home" / ".dango" / "secrets" / "metabase"
        )
        keyring = Mock()
        keyring.set_password.side_effect = RuntimeError("keychain unavailable")
        keyring.get_password.side_effect = RuntimeError("keychain unavailable")
        monkeypatch.setattr(credentials, "keyring", keyring)
        project = tmp_path / "project"
        (project / ".dango").mkdir(parents=True)
        (project / ".dango" / "project.yml").write_text(
            yaml.safe_dump(
                {"project": {"name": "T", "id": "11111111-2222-3333-4444-555555555555"}}
            ),
            encoding="utf-8",
        )
        store = MetabaseCredentialStore(project, cloud_mode=False)
        store.save("fallback-secret")
        assert store.load() == "fallback-secret"

    def test_secrets_env_file_written(self, tmp_path: Path) -> None:
        _write_env_file(tmp_path, {"KEY": "value"})
        assert "KEY=value" in (tmp_path / ".env").read_text(encoding="utf-8")
