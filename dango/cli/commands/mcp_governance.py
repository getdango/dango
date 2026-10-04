"""dango/cli/commands/mcp_governance.py

MCP governance tools: schema drift, PII findings, scans and overrides.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dango.cli.commands.mcp_helpers import _get_project_root, _git_warnings
from dango.cli.commands.mcp_server import mcp

_MAX_ROWS = 500

_TIGHTEN_ONLY = (
    "Marking a column not_pii would unmask it in MCP results; ask the user to run "
    "`dango governance pii-set {source} {table} {column} --status not_pii` or use the web UI."
)


def _mcp_pii_mask_columns(project_root: Path) -> set[str] | None:
    """Columns to mask in MCP query results, or None when api.mcp_mask_pii is False.

    Fails closed: if the config cannot be read, masking stays enabled.
    """
    from dango.config.helpers import load_config
    from dango.governance.pii_masking import get_pii_column_names

    try:
        if not load_config(project_root).api.mcp_mask_pii:
            return None
    except Exception:
        pass
    return get_pii_column_names(project_root)


def _check_source(project_root: Path, source: str) -> dict[str, Any] | None:
    """Return an error dict if `source` is not configured, else None."""
    from dango.config.helpers import load_config

    config = load_config(project_root)
    if not config or config.sources.get_source(source) is None:
        return {"error": f"Source '{source}' not found in sources.yml"}
    return None


def _effective(findings: list[dict[str, Any]], overrides: list[dict[str, Any]]) -> list[dict]:
    status = {(o["source"], o["table_name"], o["column_name"]): o["pii_status"] for o in overrides}
    return [
        {
            **f,
            "effective_status": status.get((f["source"], f["table_name"], f["column_name"]), "pii"),
        }
        for f in findings
    ]


@mcp.tool()
def get_schema_drift(
    source: str | None = None, table_name: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List schema drift events and the sources blocked by unresolved breaking drift.

    Args:
        source: Optional source name filter.
        table_name: Optional table name filter.
        limit: Max events (default 50, max 500).

    Returns dict with: events (newest first), needs_attention (sources whose dbt run is
    skipped until accept_schema_drift is called).
    """
    project_root = _get_project_root()
    from dango.governance.schema_drift import get_drift_history, get_sources_needing_attention

    try:
        if source is not None and (err := _check_source(project_root, source)):
            return err
        events = get_drift_history(
            project_root, source=source, table_name=table_name, limit=max(1, min(limit, _MAX_ROWS))
        )
        return {"events": events, "needs_attention": get_sources_needing_attention(project_root)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def accept_schema_drift(source: str) -> dict[str, Any]:
    """Accept the current schema of a source as the new baseline, unblocking dbt.

    Review get_schema_drift and update any affected models BEFORE calling this.

    Args:
        source: Source name.
    """
    project_root = _get_project_root()
    from dango.governance.schema_drift import accept_drift

    try:
        if err := _check_source(project_root, source):
            return err
        accept_drift(project_root, source)
        result: dict[str, Any] = {
            "status": "accepted",
            "source": source,
            "note": "Current schema is the new baseline; dbt will run for this source on the next sync.",
        }
        if warnings := _git_warnings(project_root):
            result["git_warning"] = warnings
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_pii_findings(
    source: str | None = None, table_name: str | None = None, limit: int = 100
) -> dict[str, Any]:
    """List cached PII scan findings with their effective status after overrides.

    Never includes sample values. Args: source / table_name filters, limit (max 500).

    Returns dict with: findings (each with effective_status "pii" or "not_pii").
    """
    project_root = _get_project_root()
    from dango.governance.pii_detector import get_pii_findings as _findings
    from dango.governance.pii_overrides import get_pii_overrides

    try:
        if source is not None and (err := _check_source(project_root, source)):
            return err
        rows = _findings(
            project_root, source=source, table_name=table_name, limit=max(1, min(limit, _MAX_ROWS))
        )
        return {"findings": _effective(rows, get_pii_overrides(project_root))}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def scan_pii(source: str, table_name: str | None = None) -> dict[str, Any]:
    """Scan a source's raw tables for PII (Presidio) and refresh the cached findings.

    First run may download the spaCy model (network access). Does not send webhooks.

    Args:
        source: Source name.
        table_name: Optional single table; must exist in raw_{source}.

    Returns dict with: tables (per-table finding counts), findings (no sample values).
    """
    project_root = _get_project_root()
    try:
        if err := _check_source(project_root, source):
            return err
        import duckdb

        from dango.governance import pii_detector

        db_path = project_root / "data" / "warehouse.duckdb"
        if not db_path.exists():
            return {"error": "No warehouse found. Run dango sync first."}
        conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
        try:
            discovered = [
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = ? AND table_name NOT LIKE '_dlt_%' "
                    "ORDER BY table_name",
                    [f"raw_{source}"],
                ).fetchall()
            ]
        finally:
            conn.close()
        if table_name is not None:
            if table_name not in discovered:
                return {"error": f"Table '{table_name}' not found in raw_{source}"}
            discovered = [table_name]
        if pii_detector._get_analyzer() is None:
            return {"error": "PII scanning unavailable: spaCy model could not be loaded"}
        findings: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for t in discovered:
            found = pii_detector.scan_table_for_pii(project_root, source, t)
            counts[t] = len(found)
            findings.extend(found)
        return {"tables": counts, "findings": findings}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_pii_overrides(source: str | None = None) -> dict[str, Any]:
    """List per-column PII overrides (pii = force-mask, not_pii = dismissed false positive)."""
    project_root = _get_project_root()
    from dango.governance.pii_overrides import get_pii_overrides

    try:
        if source is not None and (err := _check_source(project_root, source)):
            return err
        return {"overrides": get_pii_overrides(project_root, source=source)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def set_pii_override(
    source: str,
    table_name: str,
    column_name: str,
    pii_status: str = "pii",
    reason: str | None = None,
) -> dict[str, Any]:
    """Mark a raw column as PII so MCP query results mask it.

    Only pii_status="pii" is accepted over MCP: an agent may tighten but never loosen PII
    classification. Marking a column not_pii requires the user (CLI or web UI).
    """
    project_root = _get_project_root()
    from dango.governance.pii_overrides import set_pii_override as _set

    if pii_status != "pii":
        return {"error": _TIGHTEN_ONLY.format(source=source, table=table_name, column=column_name)}
    try:
        if err := _check_source(project_root, source):
            return err
        _set(project_root, source, table_name, column_name, "pii", set_by="mcp", reason=reason)
        return {
            "status": "set",
            "source": source,
            "table_name": table_name,
            "column_name": column_name,
            "pii_status": "pii",
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def delete_pii_override(source: str, table_name: str, column_name: str) -> dict[str, Any]:
    """Delete a not_pii override (re-masks the column). A pii override cannot be deleted
    over MCP because that would unmask the column; ask the user."""
    project_root = _get_project_root()
    from dango.governance.pii_overrides import delete_pii_override as _delete
    from dango.governance.pii_overrides import get_pii_overrides

    try:
        if err := _check_source(project_root, source):
            return err
        existing = [
            o
            for o in get_pii_overrides(project_root, source=source)
            if o["table_name"] == table_name and o["column_name"] == column_name
        ]
        if not existing:
            return {"status": "not_found"}
        if existing[0]["pii_status"] == "pii":
            return {
                "error": "Deleting a pii override would unmask the column in MCP results; "
                "ask the user to remove it via the web UI or by editing .dango/pii-overrides.yml."
            }
        _delete(project_root, source, table_name, column_name)
        return {"status": "deleted"}
    except Exception as e:
        return {"error": str(e)}
