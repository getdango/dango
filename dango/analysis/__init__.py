"""dango/analysis/__init__.py

Automated data analysis module.
"""

from dango.analysis.config import load_metrics_config
from dango.analysis.metrics import run_analysis

__all__: list[str] = [
    "load_metrics_config",
    "run_analysis",
]
