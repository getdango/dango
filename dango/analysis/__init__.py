"""dango/analysis/__init__.py

Automated data monitoring and analysis module.
"""

from dango.analysis.config import (
    add_monitors_to_config,
    load_monitors_config,
    save_monitors_config,
)
from dango.analysis.formatter import categorize_results, format_webhook_summary
from dango.analysis.metrics import run_analysis
from dango.analysis.models import (
    DimensionContributor,
    DrillDownDimension,
    MetricConfig,
    MetricsConfig,
    MonitorConfig,
    MonitorsConfig,
)
from dango.analysis.templates import generate_metrics_for_source

__all__: list[str] = [
    "DimensionContributor",
    "DrillDownDimension",
    "MetricConfig",
    "MetricsConfig",
    "MonitorConfig",
    "MonitorsConfig",
    "add_monitors_to_config",
    "categorize_results",
    "format_webhook_summary",
    "generate_metrics_for_source",
    "load_monitors_config",
    "run_analysis",
    "save_monitors_config",
]
