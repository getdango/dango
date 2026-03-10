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
