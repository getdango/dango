"""dango/cli/commands/doctor.py

Credential health check: cross-references configured sources against
available credentials and surfaces missing/expired tokens.
"""

import click

from dango.cli import console


@click.command("doctor")
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Check credential health for all configured sources."""
    from rich.table import Table

    from dango.cli.utils import require_project_context
    from dango.ingestion.credential_health import get_cached_credential_health

    project_root = require_project_context(ctx)
    results = get_cached_credential_health(project_root)

    if not results:
        console.print("\n[dim]No sources configured.[/dim]\n")
        return

    table = Table(title="Credential Health", show_header=True, header_style="bold")
    table.add_column("Source", style="cyan")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Detail")

    status_styles = {
        "ok": "[green]✓ OK[/green]",
        "missing": "[red]✗ Missing[/red]",
        "expired": "[red]✗ Expired[/red]",
        "expiring_soon": "[yellow]⚠ Expiring soon[/yellow]",
        "unknown": "[dim]? Unknown[/dim]",
    }

    for r in results:
        table.add_row(
            r["source"], r["type"], status_styles.get(r["status"], r["status"]), r.get("detail", "")
        )

    console.print()
    console.print(table)

    issues = [r for r in results if r["status"] != "ok"]
    if issues:
        console.print(f"\n[red]{len(issues)} issue(s) found.[/red]")
        console.print("[dim]Run `dango oauth <source>` to re-authenticate OAuth sources.[/dim]\n")
    else:
        console.print("\n[green]All credentials are healthy.[/green]\n")
