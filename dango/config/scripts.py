"""dango/config/scripts.py

Per-script configuration for the Scripts page: an optional execution timeout
override, the environment variable names the script may receive (``env:``) and
whether it writes the warehouse (``warehouse:``). Defines the ``scripts:`` section of
``.dango/scripts.yml`` as a Pydantic model, mirroring the
``timeout_minutes`` pattern already used by ``ScheduleConfig`` in
``schedules.py`` (1.0.8-BUGS-FOUND: the Scripts page previously had no
config surface at all — a hardcoded 5-minute timeout applied to every
script with no way to override it).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from dango.config.exceptions import ConfigValidationError

__all__ = [
    "check_env_name",
    "ScriptConfig",
    "ScriptsConfig",
    "load_scripts_config",
]

# Default timeout applied to any script not listed in scripts.yml, or listed
# without an explicit timeout_seconds. Matches the old, hardcoded
# _SCRIPT_TIMEOUT value that used to live in web/routes/scripts_helpers.py.
DEFAULT_SCRIPT_TIMEOUT_SECONDS = 300

#: Environment variable names a script may declare in ``env:``.
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
#: Names and prefixes the launcher owns or that change how a process loads code.
RESERVED_ENV_NAMES = frozenset({"PATH", "HOME", "USER", "SHELL", "DLT_DATA_DIR"})
RESERVED_ENV_PREFIXES = ("DANGO_", "PYTHON", "LD_", "DYLD_")


def check_env_name(name: str) -> str:
    """Return *name* if a script may declare it, else raise ``ValueError``."""
    if not ENV_NAME_RE.match(name):
        raise ValueError(f"Invalid environment variable name: {name!r} (use A-Z, 0-9, _)")
    if name in RESERVED_ENV_NAMES or name.startswith(RESERVED_ENV_PREFIXES):
        raise ValueError(f"Environment variable name is reserved: {name!r}")
    return name


class ScriptConfig(BaseModel):
    """Per-script config: relative path (matches `_discover_scripts()`'s
    `name`/`path` key), an optional timeout override, the environment variables
    the script is entitled to (``env:``) and its warehouse access
    (``warehouse: read|write``, default read)."""

    model_config = ConfigDict(frozen=True)

    path: str
    timeout_seconds: int | None = None
    env: list[str] = []
    warehouse: Literal["read", "write"] = "read"

    @field_validator("env")
    @classmethod
    def _validate_env(cls, v: list[str]) -> list[str]:
        # Duplicates collapse; order is kept so the declaration reads the same.
        return list(dict.fromkeys(check_env_name(name) for name in v))

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

    def env_for(self, script_path: str) -> tuple[str, ...]:
        """Environment variable names a script declared (empty if unlisted)."""
        for script in self.scripts:
            if script.path == script_path:
                return tuple(script.env)
        return ()

    def warehouse_for(self, script_path: str) -> Literal["read", "write"]:
        """Declared warehouse access for a script (``read`` if unlisted)."""
        for script in self.scripts:
            if script.path == script_path:
                return script.warehouse
        return "read"


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
