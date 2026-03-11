"""dango/analysis/config.py

Load, save, and validate the metrics configuration from ``.dango/metrics.yml``.

Mirrors the ``load_schedules_config()`` pattern in ``dango/config/schedules.py``.
Missing file → empty ``MetricsConfig``.  Invalid YAML or Pydantic errors →
``AnalysisConfigError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dango.analysis.models import MetricConfig, MetricsConfig
from dango.exceptions import AnalysisConfigError
from dango.logging import get_logger

logger = get_logger(__name__)


def get_metrics_file_path(project_root: Path) -> Path:
    """Return the path to ``.dango/metrics.yml``.

    Args:
        project_root: Path to the Dango project root.

    Returns:
        Absolute path to the metrics configuration file.
    """
    return project_root / ".dango" / "metrics.yml"


def load_metrics_config(project_root: Path) -> MetricsConfig:
    """Load metrics config from ``.dango/metrics.yml``.

    Returns an empty ``MetricsConfig`` if the file is missing.

    Args:
        project_root: Path to the Dango project root.

    Returns:
        Parsed metrics configuration.

    Raises:
        AnalysisConfigError: If the file exists but contains invalid data.
    """
    path = get_metrics_file_path(project_root)
    if not path.exists():
        return MetricsConfig()

    try:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise AnalysisConfigError(f"Invalid YAML in {path}:\n{e}") from e

    try:
        return MetricsConfig(**data)
    except Exception as e:
        raise AnalysisConfigError(f"Invalid metrics configuration in {path}:\n{e}") from e


def save_metrics_config(
    project_root: Path,
    config: MetricsConfig,
    *,
    header_comment: str | None = None,
) -> None:
    """Serialize ``MetricsConfig`` to ``.dango/metrics.yml``.

    Args:
        project_root: Path to the Dango project root.
        config: The metrics configuration to save.
        header_comment: Optional comment block prepended to the file.
    """
    path = get_metrics_file_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {
        "enabled": config.enabled,
        "metrics": [m.model_dump(exclude_none=True, mode="json") for m in config.metrics],
    }

    lines: list[str] = []
    if header_comment:
        for line in header_comment.splitlines():
            lines.append(f"# {line}" if line.strip() else "#")
        lines.append("")

    lines.append(yaml.dump(data, default_flow_style=False, sort_keys=False))

    path.write_text("\n".join(lines), encoding="utf-8")


def add_metrics_to_config(
    project_root: Path,
    new_metrics: list[MetricConfig],
    *,
    header_comment: str | None = None,
) -> MetricsConfig:
    """Load existing config, append new metrics (dedup by name), and save.

    Existing metrics with the same name are preserved (not overwritten).

    Args:
        project_root: Path to the Dango project root.
        new_metrics: Metrics to add.
        header_comment: Optional comment block prepended to the file.

    Returns:
        The merged ``MetricsConfig``.
    """
    existing = load_metrics_config(project_root)
    existing_names = {m.name for m in existing.metrics}
    merged = list(existing.metrics) + [m for m in new_metrics if m.name not in existing_names]
    config = MetricsConfig(enabled=existing.enabled, metrics=merged)
    save_metrics_config(project_root, config, header_comment=header_comment)
    return config
