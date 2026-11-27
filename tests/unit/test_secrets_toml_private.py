"""tests/unit/test_secrets_toml_private.py

.dlt/secrets.toml is created owner-only (POSIX), tightened on the next write, and replaced atomically.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
import toml

from dango.config.credentials import CredentialManager
from dango.config.helpers import save_config
from dango.ingestion.sources.setup_service import create_source
from dango.oauth.storage import OAuthStorage
from tests.factories.oauth_factories import make_oauth_credential
from tests.factories.scaffold_project import build_fresh_project

pytestmark = pytest.mark.unit

EXISTING = '# my own notes\n[custom]\nkeep = "fake-value-1"\n'


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _private(path: Path, before: int) -> bool:
    """0600 where POSIX mode bits exist; on Windows the mode is not enforced and not touched."""
    return _mode(path) == (0o600 if os.name == "posix" else before)


def _loose_file(root: Path) -> Path:
    path = root / ".dlt" / "secrets.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXISTING, encoding="utf-8", newline="\n")
    os.chmod(path, 0o644)
    return path


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name != "secrets.toml")


def test_a_fresh_init_creates_secrets_toml_owner_only(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = build_fresh_project(tmp_path_factory)
    path = root / ".dlt" / "secrets.toml"
    assert path.exists()
    assert _private(path, 0o644)
    assert _leftovers(path.parent) == ["config.toml"]


def test_init_dlt_directory_creates_it_owner_only(tmp_path: Path) -> None:
    CredentialManager(tmp_path).init_dlt_directory()
    assert _private(tmp_path / ".dlt" / "secrets.toml", 0o644)


def test_oauth_storage_creates_and_saves_owner_only(tmp_path: Path) -> None:
    storage = OAuthStorage(tmp_path)
    path = tmp_path / ".dlt" / "secrets.toml"
    assert _private(path, 0o644)
    assert storage.save(make_oauth_credential())
    assert _private(path, 0o644)
    assert storage.get("google_sheets") is not None
    assert _leftovers(path.parent) == []


def test_an_existing_loose_file_is_tightened_on_the_next_write_with_its_content(
    tmp_path: Path,
) -> None:
    path = _loose_file(tmp_path)
    assert OAuthStorage(tmp_path).save(make_oauth_credential())
    assert _private(path, 0o644)
    data = toml.loads(path.read_text(encoding="utf-8"))
    assert data["custom"] == {"keep": "fake-value-1"}
    assert data["sources"]["google_sheets"]["credentials"]["refresh_token"] == "test-refresh-token"


def test_reading_never_changes_the_mode(tmp_path: Path) -> None:
    path = _loose_file(tmp_path)
    storage = OAuthStorage(tmp_path)
    storage.list()
    storage.get("google_sheets")
    storage.exists("google_sheets")
    assert _mode(path) == 0o644
    assert path.read_text(encoding="utf-8") == EXISTING


def test_a_failed_serialisation_leaves_the_old_file_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _loose_file(tmp_path)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr("dango.oauth.storage.toml.dump", boom)
    monkeypatch.setattr("dango.oauth.storage.toml.dumps", boom)
    assert OAuthStorage(tmp_path).save(make_oauth_credential()) is False
    assert path.read_text(encoding="utf-8") == EXISTING
    assert _leftovers(path.parent) == []


def test_a_failed_replace_leaves_the_old_file_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _loose_file(tmp_path)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    assert OAuthStorage(tmp_path).save(make_oauth_credential()) is False
    assert path.read_text(encoding="utf-8") == EXISTING
    assert _leftovers(path.parent) == []


def test_the_source_template_is_appended_owner_only_and_keeps_existing_content(
    tmp_path: Path, sample_config: Any
) -> None:
    save_config(sample_config, tmp_path)
    path = _loose_file(tmp_path)
    create_source(tmp_path, "salesforce", "sf", {})
    assert _private(path, 0o644)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(EXISTING.rstrip())
    assert "[sources.sf.credentials]" in text
    assert _leftovers(path.parent) == []
