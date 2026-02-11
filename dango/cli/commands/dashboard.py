"""dango/cli/commands/dashboard.py

Dashboard provisioning commands.
"""

import click

from dango.cli import console


@click.group()
@click.pass_context
def dashboard(ctx: click.Context) -> None:
    """
    Provision pre-built Metabase dashboards.

    Commands:
      dango dashboard provision    Create Data Pipeline Health dashboard
    """
    pass


@dashboard.command("provision")
@click.option("--url", default="http://localhost:3001", help="Metabase URL")
@click.option("--username", default="admin@example.com", help="Metabase admin username")
@click.option("--password", prompt=True, hide_input=True, help="Metabase admin password")
@click.pass_context
def dashboard_provision(ctx: click.Context, url: str, username: str, password: str) -> None:
    """
    Provision Data Pipeline Health dashboard in Metabase.

    This creates a pre-built dashboard with:
    - Pipeline health score
    - Source sync status
    - Data freshness indicators
    - Row count trends
    - dbt test results

    The dashboard provides instant visibility into your data pipeline.

    Examples:
      dango dashboard provision                  # Use defaults (localhost:3001)
      dango dashboard provision --url http://metabase.local
    """
    from rich.panel import Panel
    from rich.table import Table

    from dango.visualization import provision_dashboard

    console.print("\n🍡 [bold]Provisioning Metabase Dashboard[/bold]\n")

    try:
        console.print(f"Connecting to Metabase at {url}...")
        console.print()

        # Provision dashboard
        with console.status("[cyan]Creating dashboard...[/cyan]", spinner="dots"):
            result = provision_dashboard(metabase_url=url, username=username, password=password)

        if result["success"]:
            console.print("[green]✅ Dashboard provisioned successfully![/green]\n")

            # Show dashboard info
            info_panel = Panel(
                f"[bold]Dashboard ID:[/bold] {result['dashboard_id']}\n"
                f"[bold]URL:[/bold] {result['dashboard_url']}\n"
                f"[bold]Cards Created:[/bold] {len(result['cards_created'])}",
                title="📊 Data Pipeline Health Dashboard",
                border_style="green",
            )
            console.print(info_panel)
            console.print()

            # Show created cards
            if result["cards_created"]:
                console.print("[bold]Created Visualizations:[/bold]\n")
                table = Table(show_header=False)
                table.add_column("Card", style="cyan")

                for card in result["cards_created"]:
                    table.add_row(f"✓ {card['name']}")

                console.print(table)
                console.print()

            # Show errors if any
            if result["errors"]:
                console.print("[yellow]⚠️  Warnings:[/yellow]")
                for error in result["errors"]:
                    console.print(f"  • {error}")
                console.print()

            # Next steps
            console.print("[cyan]Next steps:[/cyan]")
            console.print(f"  1. Open dashboard: {result['dashboard_url']}")
            console.print("  2. Customize visualizations as needed")
            console.print("  3. Share with your team")
            console.print()

        else:
            console.print("[red]❌ Dashboard provisioning failed[/red]\n")

            if result["errors"]:
                console.print("[red]Errors:[/red]")
                for error in result["errors"]:
                    console.print(f"  • {error}")
                console.print()

            console.print("[yellow]Troubleshooting:[/yellow]")
            console.print("  • Ensure Metabase is running: dango start")
            console.print(f"  • Check Metabase is accessible: {url}")
            console.print("  • Verify admin credentials are correct")
            console.print("  • Check DuckDB database is connected in Metabase")

            raise click.Abort()

    except KeyboardInterrupt:
        console.print("\n[yellow]Provisioning cancelled[/yellow]")
        raise click.Abort() from None
    except Exception as e:
        console.print(f"\n[red]Error:[/red] {e}")
        import traceback

        console.print(traceback.format_exc())
        raise click.Abort() from e
