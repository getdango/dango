"""dango/governance/pii_masking.py

Resolve PII-flagged column names and mask them in tabular query results.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dango.logging import get_logger

logger = get_logger(__name__)

MASK_VALUE = "[PII masked]"

_NOTE_LIMITS = (
    "Masking matches output column names (case-insensitive) against columns flagged as PII "
    "in raw tables, so an aliased column (email AS contact) or an expression-derived name "
    "(upper(email)) is NOT masked. Filtering on a masked column (WHERE email LIKE 'a%') and "
    "aggregates over it can still reveal information. This is a guardrail against accidental "
    "exposure, not a security boundary. Disable with api.mcp_mask_pii: false in project.yml."
)
_NOTE_NO_DATA = (
    "No PII scan results or overrides exist for this project, so nothing was masked. "
    "Run scan_pii (or sync a source) to detect PII columns. " + _NOTE_LIMITS
)


def get_pii_column_names(project_root: Path) -> set[str]:
    """Lower-cased column names currently classified as PII.

    flagged = {(source, table, column) from get_pii_findings(project_root, limit=100_000)}
              - {overrides with pii_status == "not_pii"}
              + {overrides with pii_status == "pii"}
    Returns {column.lower() for (_, _, column) in flagged}.
    Missing governance DB / overrides file → empty set (never raises).
    """
    findings: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    try:
        from dango.governance.pii_detector import get_pii_findings

        findings = get_pii_findings(project_root, limit=100_000)
    except Exception:
        logger.debug("pii_masking_findings_unavailable", exc_info=True)
    try:
        from dango.governance.pii_overrides import get_pii_overrides

        overrides = get_pii_overrides(project_root)
    except Exception:
        logger.debug("pii_masking_overrides_unavailable", exc_info=True)

    def _key(row: dict[str, Any]) -> tuple[str, str, str]:
        return (row["source"], row["table_name"], row["column_name"])

    flagged = {_key(f) for f in findings}
    flagged -= {_key(o) for o in overrides if o["pii_status"] == "not_pii"}
    flagged |= {_key(o) for o in overrides if o["pii_status"] == "pii"}
    return {column.lower() for (_, _, column) in flagged}


def mask_query_result(result: dict[str, Any], pii_columns: set[str]) -> dict[str, Any]:
    """Return a copy of an MCP query result with PII columns masked.

    `result` has keys columns (list[str]) and rows (list[list[Any]]); any other keys pass
    through. Non-null values in a masked column become MASK_VALUE; None stays None.
    Adds result["pii_masking"] = {"enabled": True, "masked_columns": [...in column order...],
    "note": ...} where note explains name-based matching, or says no PII scan results exist
    when pii_columns is empty.
    A result with an "error" key is returned unchanged.
    """
    if "error" in result:
        return result
    columns: list[str] = list(result.get("columns", []))
    indexes = [i for i, name in enumerate(columns) if name.lower() in pii_columns]
    masked = set(indexes)
    rows = [
        [MASK_VALUE if i in masked and v is not None else v for i, v in enumerate(row)]
        for row in result.get("rows", [])
    ]
    return {
        **result,
        "rows": rows,
        "pii_masking": {
            "enabled": True,
            "masked_columns": [columns[i] for i in indexes],
            "note": _NOTE_LIMITS if pii_columns else _NOTE_NO_DATA,
        },
    }
