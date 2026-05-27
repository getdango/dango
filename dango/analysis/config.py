"""dango/analysis/config.py

Load, save, and validate the monitors configuration from ``.dango/monitors.yml``.

Mirrors the ``load_schedules_config()`` pattern in ``dango/config/schedules.py``.
Missing file → empty ``MonitorsConfig``.  Invalid YAML or Pydantic errors →
``AnalysisConfigError``.

Backward compatibility: falls back to ``.dango/metrics.yml`` if
``.dango/monitors.yml`` does not exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dango.analysis.models import MonitorConfig, MonitorsConfig
from dango.exceptions import AnalysisConfigError
from dango.logging import get_logger

logger = get_logger(__name__)


def get_monitors_file_path(project_root: Path) -> Path:
    """Return the path to ``.dango/monitors.yml``.

    Args:
        project_root: Path to the Dango project root.

    Returns:
        Absolute path to the monitors configuration file.
    """
    return project_root / ".dango" / "monitors.yml"


def load_monitors_config(project_root: Path) -> MonitorsConfig:
    """Load monitors config from ``.dango/monitors.yml`` (or legacy ``metrics.yml``).

    Returns an empty ``MonitorsConfig`` if the file is missing.

    Args:
        project_root: Path to the Dango project root.

    Returns:
        Parsed monitors configuration.

    Raises:
        AnalysisConfigError: If the file exists but contains invalid data.
    """
    path = get_monitors_file_path(project_root)
    if not path.exists():
        return MonitorsConfig()

    try:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise AnalysisConfigError(f"Invalid YAML in {path}:\n{e}") from e

    try:
        return MonitorsConfig(**data)
    except Exception as e:
        raise AnalysisConfigError(f"Invalid monitors configuration in {path}:\n{e}") from e


def save_monitors_config(
    project_root: Path,
    config: MonitorsConfig,
    *,
    header_comment: str | None = None,
) -> None:
    """Serialize ``MonitorsConfig`` to ``.dango/monitors.yml``.

    Args:
        project_root: Path to the Dango project root.
        config: The monitors configuration to save.
        header_comment: Optional comment block prepended to the file.
    """
    path = project_root / ".dango" / "monitors.yml"
    path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {
        "enabled": config.enabled,
        "monitors": [m.model_dump(exclude_none=True, mode="json") for m in config.monitors],
    }

    lines: list[str] = []
    if header_comment:
        for line in header_comment.splitlines():
            lines.append(f"# {line}" if line.strip() else "#")
        lines.append("")

    lines.append(yaml.dump(data, default_flow_style=False, sort_keys=False))

    path.write_text("\n".join(lines), encoding="utf-8")


def add_monitors_to_config(
    project_root: Path,
    new_monitors: list[MonitorConfig],
    *,
    header_comment: str | None = None,
) -> MonitorsConfig:
    """Load existing config, append new monitors (dedup by name), and save.

    Existing monitors with the same name are preserved (not overwritten).

    Args:
        project_root: Path to the Dango project root.
        new_monitors: Monitors to add.
        header_comment: Optional comment block prepended to the file.

    Returns:
        The merged ``MonitorsConfig``.
    """
    existing = load_monitors_config(project_root)
    existing_names = {m.name for m in existing.monitors}
    merged = list(existing.monitors) + [m for m in new_monitors if m.name not in existing_names]
    config = MonitorsConfig(enabled=existing.enabled, monitors=merged)
    save_monitors_config(project_root, config, header_comment=header_comment)
    return config
