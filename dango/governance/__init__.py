"""dango/governance/__init__.py

Data governance module: schema drift detection and PII scanning.
"""

from dango.governance.models import (
    DriftEvent,
    DriftResponse,
    PiiFinding,
    PiiOverride,
    PiiOverrideRequest,
    PiiOverridesResponse,
    PiiResponse,
)
from dango.governance.pii_detector import (
    get_pii_findings,
    scan_sources_for_pii,
    scan_table_for_pii,
)
from dango.governance.pii_overrides import (
    delete_pii_override,
    get_pii_overrides,
    set_pii_override,
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
    "PiiOverride",
    "PiiOverrideRequest",
    "PiiOverridesResponse",
    "PiiResponse",
    "delete_pii_override",
    "detect_drift_for_sources",
    "detect_table_drift",
    "get_drift_history",
    "get_pii_findings",
    "get_pii_overrides",
    "scan_sources_for_pii",
    "scan_table_for_pii",
    "set_pii_override",
]
