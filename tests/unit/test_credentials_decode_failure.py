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


def test_model_docs_load_names_the_encoding_cause(tmp_path: Path) -> None:
    from dango.transformation.model_common import ModelServiceError
    from dango.transformation.model_docs import _load_yaml

    path = tmp_path / "schema.yml"
    path.write_bytes(b"models:\n  - name: m\n    description: caf\xe9\n")

    with pytest.raises(ModelServiceError) as info:
        _load_yaml(path)

    assert "UTF-8" in str(info.value)
    assert "schema.yml" in str(info.value)


def test_model_graph_skips_an_undecodable_model_file(tmp_path: Path) -> None:
    from dango.transformation.model_sql import _find_cycle

    models = tmp_path / "dbt" / "models" / "marts"
    models.mkdir(parents=True)
    (models / "orders.sql").write_bytes(b"-- caf\xe9\nselect 1\n")

    assert _find_cycle(tmp_path, "new_model", ["orders"]) is None


_BAD_TOML = b'[sources.a]\nkey = "unterminated\n\n[sources.b]\ntoken = "keep-me"\n'


def test_toml_syntax_error_is_non_destructive(manager: CredentialManager) -> None:
    manager.secrets_file.write_bytes(_BAD_TOML)
    manager.config_file.write_bytes(_BAD_TOML)

    with pytest.raises(ConfigError) as info:
        manager.load_secrets()
    assert "secrets.toml" in str(info.value)
    assert "TOML" in str(info.value)
    with pytest.raises(ConfigError):
        manager.save_secrets({"sources": {"c": {"key": "new"}}})
    with pytest.raises(ConfigError):
        manager.load_config()
    with pytest.raises(ConfigError):
        manager.save_config({"runtime": {"x": 1}})

    assert manager.secrets_file.read_bytes() == _BAD_TOML
    assert manager.config_file.read_bytes() == _BAD_TOML


def test_remove_model_skips_an_undecodable_downstream_file(tmp_path: Path) -> None:
    from dango.transformation.model_service import remove_model

    marts = tmp_path / "dbt" / "models" / "marts"
    marts.mkdir(parents=True)
    (marts / "target_model.sql").write_text("select 1\n", encoding="utf-8")
    (marts / "other.sql").write_bytes(b"-- caf\xe9\nselect 1\n")

    result = remove_model(tmp_path, "target_model", drop_table=False, dry_run=True)

    assert result is not None
    assert (marts / "target_model.sql").exists()


def test_mcp_docs_read_yaml_treats_an_undecodable_file_as_empty(tmp_path: Path) -> None:
    from dango.cli.commands.mcp_docs import _read_yaml

    path = tmp_path / "x.yml"
    path.write_bytes(b"description: caf\xe9\n")

    assert _read_yaml(path) == {}


def test_locate_model_docs_skips_an_undecodable_yaml(tmp_path: Path) -> None:
    from dango.transformation.model_docs import locate_model_docs

    models = tmp_path / "dbt" / "models" / "marts"
    models.mkdir(parents=True)
    (models / "broken.yml").write_bytes(b"models:\n  - name: m\n    description: caf\xe9\n")
    (models / "good.yml").write_text("models:\n  - name: m\n", encoding="utf-8")

    assert locate_model_docs(tmp_path, "m") == models / "good.yml"


def test_remote_auth_timeout_script_names_its_encoding(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    from dango.cli.commands.deploy_provision import _build_auth_timeout_script

    script = _build_auth_timeout_script()
    assert script.count("encoding='utf-8'") == 2

    (tmp_path / ".dango").mkdir()
    project = tmp_path / ".dango" / "project.yml"
    project.write_bytes("project:\n  name: café\n".encode())
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C"}
    subprocess.run(
        [sys.executable, "-c", script.replace("/srv/dango/project", str(tmp_path))],
        check=True,
        cwd=tmp_path,
        env={**env, "PYTHONPATH": os.pathsep.join(sys.path)},
    )

    text = project.read_bytes().decode("utf-8")
    assert "café" in text
    assert "session_max_days: 30" in text
