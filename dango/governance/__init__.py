"""dango/governance/__init__.py

Data governance module: schema drift detection and PII scanning.
"""

from dango.governance.models import (
    AcceptDriftResponse,
    DriftEvent,
    DriftResponse,
    PiiFinding,
    PiiOverride,
    PiiOverridesResponse,
    PiiResponse,
    SourceAttention,
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
    accept_drift,
    detect_drift_for_sources,
    detect_table_drift,
    get_drift_history,
    get_sources_needing_attention,
)

__all__ = [
    "AcceptDriftResponse",
    "DriftEvent",
    "DriftResponse",
    "PiiFinding",
    "PiiOverride",
    "PiiOverridesResponse",
    "PiiResponse",
    "SourceAttention",
    "accept_drift",
    "delete_pii_override",
    "detect_drift_for_sources",
    "detect_table_drift",
    "get_drift_history",
    "get_pii_findings",
    "get_pii_overrides",
    "get_sources_needing_attention",
    "scan_sources_for_pii",
    "scan_table_for_pii",
    "set_pii_override",
]
