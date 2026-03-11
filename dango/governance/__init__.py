"""dango/governance/__init__.py

Data governance module: schema drift detection and PII scanning.
"""

from dango.governance.models import DriftEvent, DriftResponse, PiiFinding, PiiResponse
from dango.governance.pii_detector import (
    get_pii_findings,
    scan_sources_for_pii,
    scan_table_for_pii,
)
from dango.governance.schema_drift import (
    detect_drift_for_sources,
    detect_table_drift,
    get_drift_history,
)

__all__ = [
    "DriftEvent",
    "DriftResponse",
    "PiiFinding",
    "PiiResponse",
    "detect_drift_for_sources",
    "detect_table_drift",
    "get_drift_history",
    "get_pii_findings",
    "scan_sources_for_pii",
    "scan_table_for_pii",
]
