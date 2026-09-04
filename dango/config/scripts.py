"""dango/config/scripts.py

Per-script configuration for the Scripts page — currently just an optional
execution timeout override. Defines the ``scripts:`` section of
``.dango/scripts.yml`` as a Pydantic model, mirroring the
``timeout_minutes`` pattern already used by ``ScheduleConfig`` in
``schedules.py`` (1.0.8-BUGS-FOUND: the Scripts page previously had no
config surface at all — a hardcoded 5-minute timeout applied to every
script with no way to override it).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from dango.config.exceptions import ConfigValidationError

__all__ = [
    "ScriptConfig",
    "ScriptsConfig",
    "load_scripts_config",
]

# Default timeout applied to any script not listed in scripts.yml, or listed
# without an explicit timeout_seconds. Matches the old, hardcoded
# _SCRIPT_TIMEOUT value that used to live in web/routes/scripts_helpers.py.
DEFAULT_SCRIPT_TIMEOUT_SECONDS = 300


class ScriptConfig(BaseModel):
    """Per-script config: relative path (matches `_discover_scripts()`'s
    `name`/`path` key) plus an optional timeout override."""

    model_config = ConfigDict(frozen=True)

    path: str
    timeout_seconds: int | None = None

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout_seconds(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            msg = f"timeout_seconds must be positive. Got: {v!r}"
            raise ValueError(msg)
        return v


class ScriptsConfig(BaseModel):
    """Top-level ``.dango/scripts.yml`` schema."""

    scripts: list[ScriptConfig] = []

    def timeout_for(self, script_path: str) -> int:
        """Effective timeout for a script, falling back to the default."""
        for script in self.scripts:
            if script.path == script_path:
                return script.timeout_seconds or DEFAULT_SCRIPT_TIMEOUT_SECONDS
        return DEFAULT_SCRIPT_TIMEOUT_SECONDS


def load_scripts_config(project_root: Path) -> ScriptsConfig:
    """Load script config from ``.dango/scripts.yml``.

    Returns an empty ``ScriptsConfig`` (every script gets the default
    timeout) if the file is missing.

    Raises:
        ConfigValidationError: If the file exists but contains invalid data.
    """
    path = project_root / ".dango" / "scripts.yml"
    if not path.exists():
        return ScriptsConfig()

    try:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigValidationError(f"Invalid YAML in {path}:\n{e}") from e

    scripts_data = data.get("scripts")
    if scripts_data is None:
        return ScriptsConfig()

    try:
        return ScriptsConfig(scripts=scripts_data)
    except Exception as e:
        raise ConfigValidationError(f"Invalid script config in {path}:\n{e}") from e
