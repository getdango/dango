"""dango/analysis/__init__.py

Automated data analysis module.
"""

from dango.analysis.config import (
    add_metrics_to_config,
    load_metrics_config,
    save_metrics_config,
)
from dango.analysis.formatter import categorize_results, format_webhook_summary
from dango.analysis.metrics import run_analysis
from dango.analysis.models import DimensionContributor, DrillDownDimension
from dango.analysis.templates import generate_metrics_for_source

__all__: list[str] = [
    "DimensionContributor",
    "DrillDownDimension",
    "add_metrics_to_config",
    "categorize_results",
    "format_webhook_summary",
    "generate_metrics_for_source",
    "load_metrics_config",
    "run_analysis",
    "save_metrics_config",
]
