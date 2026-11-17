"""tests/unit/test_loader_encoding_errors.py

ConfigLoader names the encoding cause when a config file is not valid UTF-8.
"""

from pathlib import Path

import pytest

from dango.config.loader import ConfigLoader
from dango.exceptions import ConfigError

pytestmark = pytest.mark.unit

# 0xE9 (e-acute in cp1252) is not valid UTF-8 on its own.
_CP1252_BYTES = b"project:\n  name: caf\xe9-secret-value\n"


def _assert_encoding_message(exc: ConfigError, path: Path) -> None:
    text = f"{exc} {getattr(exc, 'user_message', '')}"
    assert path.name in text
    assert "UTF-8" in text
    assert "re-save" in text.lower()
    assert "secret-value" not in text


def test_load_yaml_names_encoding_cause(tmp_path: Path) -> None:
    path = tmp_path / "project.yml"
    path.write_bytes(_CP1252_BYTES)

    with pytest.raises(ConfigError) as info:
        ConfigLoader(tmp_path).load_yaml(path)

    _assert_encoding_message(info.value, path)


def test_load_cloud_config_names_encoding_cause(tmp_path: Path) -> None:
    (tmp_path / ".dango").mkdir()
    loader = ConfigLoader(tmp_path)
    loader.cloud_file.write_bytes(_CP1252_BYTES)

    with pytest.raises(ConfigError) as info:
        loader.load_cloud_config()

    _assert_encoding_message(info.value, loader.cloud_file)
