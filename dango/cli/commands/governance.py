"""dango/cli/commands/governance.py

Data governance CLI commands.
"""

from __future__ import annotations

import click

from dango.cli import console


@click.group()
def governance() -> None:
    """Data governance commands."""


@governance.command("drift-report")
@click.option("--source", default=None, help="Filter by source name.")
@click.option("--table", default=None, help="Filter by table name.")
@click.option("--limit", default=50, type=int, help="Max events to show.")
@click.pass_context
def drift_report(
    ctx: click.Context,
    source: str | None,
    table: str | None,
    limit: int,
) -> None:
    """Show schema drift events."""
    from rich.table import Table

    from dango.cli.utils import require_project_context
    from dango.governance.schema_drift import get_drift_history

    project_root = require_project_context(ctx)

    events = get_drift_history(
        project_root,
        source=source,
        table_name=table,
        limit=limit,
    )

    if not events:
        console.print("[dim]No drift events found.[/dim]")
        return

    tbl = Table(title="Schema Drift Events")
    tbl.add_column("ID", style="dim")
    tbl.add_column("Source", style="bold")
    tbl.add_column("Table")
    tbl.add_column("Column")
    tbl.add_column("Event")
    tbl.add_column("Detail")
    tbl.add_column("Detected At")

    for ev in events:
        tbl.add_row(
            str(ev["id"]),
            ev["source"],
            ev["table_name"],
            ev.get("column_name") or "-",
            ev["event_type"],
            ev.get("detail") or "-",
            ev["detected_at"],
        )

    console.print(tbl)


@governance.command("pii-report")
@click.option("--source", default=None, help="Filter by source name.")
@click.option("--table", default=None, help="Filter by table name.")
@click.option("--limit", default=50, type=int, help="Max findings to show.")
@click.pass_context
def pii_report(
    ctx: click.Context,
    source: str | None,
    table: str | None,
    limit: int,
) -> None:
    """Show PII findings."""
    from rich.table import Table

    from dango.cli.utils import require_project_context
    from dango.governance.pii_detector import get_pii_findings

    project_root = require_project_context(ctx)

    findings = get_pii_findings(
        project_root,
        source=source,
        table_name=table,
        limit=limit,
    )

    if not findings:
        console.print("[dim]No PII findings found.[/dim]")
        return

    tbl = Table(title="PII Findings")
    tbl.add_column("ID", style="dim")
    tbl.add_column("Source", style="bold")
    tbl.add_column("Table")
    tbl.add_column("Column")
    tbl.add_column("Entity Type")
    tbl.add_column("Confidence")
    tbl.add_column("Samples")
    tbl.add_column("Scanned At")

    for f in findings:
        confidence = f"{f['confidence']:.2f}" if f.get("confidence") is not None else "-"
        samples = str(f["sample_count"]) if f.get("sample_count") is not None else "-"
        tbl.add_row(
            str(f["id"]),
            f["source"],
            f["table_name"],
            f["column_name"],
            f["entity_type"],
            confidence,
            samples,
            f["scanned_at"],
        )

    console.print(tbl)


@governance.command("pii-set")
@click.argument("source")
@click.argument("table")
@click.argument("column")
@click.option(
    "--status",
    "pii_status",
    required=True,
    type=click.Choice(["pii", "not_pii"]),
    help="PII status to set.",
)
@click.option("--reason", default=None, help="Reason for the override.")
@click.pass_context
def pii_set(
    ctx: click.Context,
    source: str,
    table: str,
    column: str,
    pii_status: str,
    reason: str | None,
) -> None:
    """Set a PII override for a column."""
    from dango.cli.utils import require_project_context
    from dango.governance.pii_overrides import set_pii_override

    project_root = require_project_context(ctx)

    set_pii_override(
        project_root,
        source,
        table,
        column,
        pii_status,
        "cli",
        reason,
    )
    console.print(f"[green]Set PII override:[/green] {source}.{table}.{column} = {pii_status}")


@governance.command("pii-list")
@click.option("--source", default=None, help="Filter by source name.")
@click.pass_context
def pii_list(ctx: click.Context, source: str | None) -> None:
    """List PII overrides."""
    from rich.table import Table

    from dango.cli.utils import require_project_context
    from dango.governance.pii_overrides import get_pii_overrides

    project_root = require_project_context(ctx)

    overrides = get_pii_overrides(project_root, source=source)

    if not overrides:
        console.print("[dim]No PII overrides found.[/dim]")
        return

    tbl = Table(title="PII Overrides")
    tbl.add_column("Source", style="bold")
    tbl.add_column("Table")
    tbl.add_column("Column")
    tbl.add_column("Status")
    tbl.add_column("Set By")
    tbl.add_column("Reason")
    tbl.add_column("Updated At")

    for o in overrides:
        status_style = "green" if o["pii_status"] == "not_pii" else "red"
        tbl.add_row(
            o["source"],
            o["table_name"],
            o["column_name"],
            f"[{status_style}]{o['pii_status']}[/{status_style}]",
            o["set_by"],
            o.get("reason") or "-",
            o["updated_at"],
        )

    console.print(tbl)


@governance.command("accept")
@click.argument("source")
@click.pass_context
def accept(ctx: click.Context, source: str) -> None:
    """Accept schema drift for a source and resume dbt."""
    from dango.cli.utils import require_project_context
    from dango.governance.schema_drift import accept_drift

    project_root = require_project_context(ctx)
    accept_drift(project_root, source)
    console.print(f"[green]Schema changes accepted for '{source}'.[/green]")
    console.print("[dim]dbt will run on next sync.[/dim]")
