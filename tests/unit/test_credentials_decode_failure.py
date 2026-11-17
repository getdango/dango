"""tests/unit/test_credentials_decode_failure.py

A .dlt TOML file that is not valid UTF-8 is never silently replaced by an empty or partial one.
"""

from pathlib import Path

import pytest

from dango.config.credentials import CredentialManager
from dango.exceptions import ConfigError

pytestmark = pytest.mark.unit

# 0xE9 (e-acute in cp1252) is not valid UTF-8 on its own.
_CP1252_SECRETS = b'[sources.a]\nkey = "caf\xe9"\n\n[sources.b]\ntoken = "keep-me"\n'


@pytest.fixture
def manager(tmp_path: Path) -> CredentialManager:
    (tmp_path / ".dlt").mkdir()
    return CredentialManager(tmp_path)


def test_load_secrets_raises_naming_the_encoding(manager: CredentialManager) -> None:
    manager.secrets_file.write_bytes(_CP1252_SECRETS)

    with pytest.raises(ConfigError) as info:
        manager.load_secrets()

    text = f"{info.value} {getattr(info.value, 'user_message', '')}"
    assert "secrets.toml" in text
    assert "UTF-8" in text
    assert "keep-me" not in text


def test_save_secrets_does_not_overwrite_an_undecodable_file(manager: CredentialManager) -> None:
    manager.secrets_file.write_bytes(_CP1252_SECRETS)

    with pytest.raises(ConfigError):
        manager.save_secrets({"sources": {"c": {"key": "new"}}})

    assert manager.secrets_file.read_bytes() == _CP1252_SECRETS


def test_delete_source_credentials_does_not_overwrite_an_undecodable_file(
    manager: CredentialManager,
) -> None:
    manager.secrets_file.write_bytes(_CP1252_SECRETS)

    with pytest.raises(ConfigError):
        manager.delete_source_credentials("a")

    assert manager.secrets_file.read_bytes() == _CP1252_SECRETS


def test_config_toml_decode_failure_is_non_destructive(manager: CredentialManager) -> None:
    original = b'[runtime]\nlog_level = "caf\xe9"\n'
    manager.config_file.write_bytes(original)

    with pytest.raises(ConfigError):
        manager.load_config()
    with pytest.raises(ConfigError):
        manager.save_config({"runtime": {"x": 1}})

    assert manager.config_file.read_bytes() == original


def test_missing_files_still_load_empty_and_save_normally(manager: CredentialManager) -> None:
    assert manager.load_secrets() == {}
    assert manager.load_config() == {}

    manager.save_secrets({"sources": {"s": {"key": "v"}}})
    manager.save_config({"runtime": {"x": 1}})

    assert manager.load_secrets() == {"sources": {"s": {"key": "v"}}}
    assert manager.load_config() == {"runtime": {"x": 1}}


def test_valid_utf8_non_ascii_file_still_merges(manager: CredentialManager) -> None:
    manager.secrets_file.write_bytes('[sources.a]\nkey = "café"\n'.encode())

    manager.save_secrets({"sources": {"b": {"key": "new"}}})

    assert manager.load_secrets() == {"sources": {"a": {"key": "café"}, "b": {"key": "new"}}}
