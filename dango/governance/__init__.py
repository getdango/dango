"""dango/governance/__init__.py

Data governance module: schema drift detection and PII scanning.
"""

from dango.governance.models import DriftEvent, DriftResponse
from dango.governance.schema_drift import (
    detect_drift_for_sources,
    detect_table_drift,
    get_drift_history,
)

__all__ = [
    "DriftEvent",
    "DriftResponse",
    "detect_drift_for_sources",
    "detect_table_drift",
    "get_drift_history",
]
