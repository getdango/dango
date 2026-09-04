"""tests/unit/test_config_scripts.py

Tests for dango.config.scripts — per-script timeout config
(1.0.8-BUGS-FOUND: the Scripts page previously had a hardcoded,
unconfigurable 5-minute timeout with no config surface at all).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.unit
class TestScriptConfigValidation:
    def test_positive_timeout_accepted(self) -> None:
        from dango.config.scripts import ScriptConfig

        config = ScriptConfig(path="orchestrator_v2.py", timeout_seconds=1800)
        assert config.timeout_seconds == 1800

    def test_none_timeout_accepted(self) -> None:
        from dango.config.scripts import ScriptConfig

        config = ScriptConfig(path="quick_util.py")
        assert config.timeout_seconds is None

    def test_zero_timeout_rejected(self) -> None:
        from dango.config.scripts import ScriptConfig

        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            ScriptConfig(path="x.py", timeout_seconds=0)

    def test_negative_timeout_rejected(self) -> None:
        from dango.config.scripts import ScriptConfig

        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            ScriptConfig(path="x.py", timeout_seconds=-1)


@pytest.mark.unit
class TestScriptsConfigTimeoutFor:
    def test_configured_script_uses_override(self) -> None:
        from dango.config.scripts import ScriptConfig, ScriptsConfig

        config = ScriptsConfig(
            scripts=[ScriptConfig(path="orchestrator_v2.py", timeout_seconds=1800)]
        )
        assert config.timeout_for("orchestrator_v2.py") == 1800

    def test_unconfigured_script_uses_default(self) -> None:
        from dango.config.scripts import DEFAULT_SCRIPT_TIMEOUT_SECONDS, ScriptsConfig

        config = ScriptsConfig()
        assert config.timeout_for("anything.py") == DEFAULT_SCRIPT_TIMEOUT_SECONDS

    def test_listed_without_timeout_uses_default(self) -> None:
        from dango.config.scripts import (
            DEFAULT_SCRIPT_TIMEOUT_SECONDS,
            ScriptConfig,
            ScriptsConfig,
        )

        config = ScriptsConfig(scripts=[ScriptConfig(path="x.py")])
        assert config.timeout_for("x.py") == DEFAULT_SCRIPT_TIMEOUT_SECONDS


@pytest.mark.unit
class TestLoadScriptsConfig:
    def test_missing_file_returns_empty_config(self, tmp_path: Path) -> None:
        from dango.config.scripts import load_scripts_config

        config = load_scripts_config(tmp_path)
        assert config.scripts == []

    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        from dango.config.scripts import load_scripts_config

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "scripts.yml").write_text(
            "scripts:\n  - path: orchestrator_v2.py\n    timeout_seconds: 1800\n"
        )

        config = load_scripts_config(tmp_path)
        assert config.timeout_for("orchestrator_v2.py") == 1800

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        from dango.config.exceptions import ConfigValidationError
        from dango.config.scripts import load_scripts_config

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "scripts.yml").write_text("scripts:\n  - path: x.py\n  bad indent: [\n")

        with pytest.raises(ConfigValidationError):
            load_scripts_config(tmp_path)

    def test_invalid_timeout_raises(self, tmp_path: Path) -> None:
        from dango.config.exceptions import ConfigValidationError
        from dango.config.scripts import load_scripts_config

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "scripts.yml").write_text(
            "scripts:\n  - path: x.py\n    timeout_seconds: -5\n"
        )

        with pytest.raises(ConfigValidationError):
            load_scripts_config(tmp_path)
