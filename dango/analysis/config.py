"""dango/analysis/config.py

Load and validate the metrics configuration from ``.dango/metrics.yml``.

Mirrors the ``load_schedules_config()`` pattern in ``dango/config/schedules.py``.
Missing file → empty ``MetricsConfig``.  Invalid YAML or Pydantic errors →
``AnalysisConfigError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dango.analysis.models import MetricsConfig
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
