"""dango/cli/commands/analyze.py

CLI commands for running monitor analysis and displaying results.

Provides ``dango monitor run`` as the canonical command and
``dango analyze`` as a backward-compatible alias.
"""

from __future__ import annotations

import click

from dango.cli import console


@click.group("monitor")
def monitor() -> None:
    """Monitor data quality."""


@monitor.command("run")
@click.option("--source", default=None, help="Filter by source name.")
@click.pass_context
def monitor_run(ctx: click.Context, source: str | None) -> None:
    """Run monitor analysis and display results."""
    from rich.table import Table

    from dango.analysis.formatter import categorize_results
    from dango.analysis.metrics import run_analysis
    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)

    source_filter: list[str] | None = None
    if source is not None:
        from dango.validation import validate_identifier

        validated = validate_identifier(source)
        source_filter = [f"raw_{validated}"]

    results = run_analysis(project_root, source_filter=source_filter)

    if not results:
        console.print("[dim]No monitors configured or no data available.[/dim]")
        return

    categorized = categorize_results(results)

    tbl = Table(title="Monitor Results")
    tbl.add_column("Status")
    tbl.add_column("Monitor", style="bold")
    tbl.add_column("Value", justify="right")
    tbl.add_column("Change", justify="right")
    tbl.add_column("Trend")

    status_styles = {
        "flagged": "[red]flagged[/red]",
        "trending": "[yellow]trending[/yellow]",
        "normal": "[green]normal[/green]",
        "error": "[dim]error[/dim]",
    }

    for item in categorized:
        status_label = status_styles.get(item["status"], item["status"])
        value_str = f"{item['value']:.2f}" if item["value"] is not None else "-"
        change_str = "-"
        if item["change_pct"] is not None:
            sign = "+" if item["change_pct"] > 0 else ""
            change_str = f"{sign}{item['change_pct']:.1f}%"
        trend_str = item.get("trend_direction") or "-"

        tbl.add_row(status_label, item["name"], value_str, change_str, trend_str)

    console.print(tbl)

    # Show drill-down details for flagged metrics
    flagged = [m for m in categorized if m["status"] == "flagged"]
    for item in flagged:
        if not item["drill_down"]:
            continue
        console.print(f"\n  [red]Drill-down:[/red] {item['name']}")
        for dim in item["drill_down"]:
            console.print(f"    [bold]{dim['dimension']}[/bold]")
            for contrib in dim["contributors"][:5]:
                group = contrib["group_value"] or "(null)"
                cpct = (
                    f"{contrib['change_pct']:+.1f}%" if contrib["change_pct"] is not None else "-"
                )
                console.print(f"      {group}: {cpct}")


@click.command("analyze")
@click.option("--source", default=None, help="Filter by source name.")
@click.pass_context
def analyze(ctx: click.Context, source: str | None) -> None:
    """Run monitor analysis and display results (alias for 'monitor run')."""
    ctx.invoke(monitor_run, source=source)
