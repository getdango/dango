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
@click.option("--url", default=None, help="Metabase URL (default: project's configured port)")
@click.option(
    "--username", default=None, help="Metabase admin username (auto-detected from auth DB)"
)
@click.option("--password", prompt=True, hide_input=True, help="Metabase admin password")
@click.pass_context
def dashboard_provision(
    ctx: click.Context, url: str | None, username: str | None, password: str
) -> None:
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
      dango dashboard provision                  # Use the project's configured Metabase port
      dango dashboard provision --url http://metabase.local
    """
    from rich.panel import Panel
    from rich.table import Table

    from dango.visualization import provision_dashboard

    project_root = ctx.obj["project_root"]

    # Read the project's actual configured Metabase port rather than
    # defaulting to localhost:3000 — a project configured on a non-default
    # port would otherwise have this command silently target the wrong (or a
    # different project's) Metabase instance. Same pattern as
    # setup_metabase_if_needed() in platform/common/startup.py.
    if url is None:
        metabase_port = 3000
        try:
            from dango.config.helpers import load_config

            metabase_port = load_config(project_root).platform.metabase_port
        except Exception:  # noqa: BLE001
            pass
        url = f"http://localhost:{metabase_port}"

    # Resolve admin email: env var → auth DB → fallback
    if username is None:
        import os

        username = os.environ.get("DANGO_ADMIN_EMAIL", "")
        if not username:
            try:
                from dango.auth.admin import get_auth_db_path
                from dango.auth.database import list_users
                from dango.auth.models import Role

                db_path = get_auth_db_path(project_root)
                if db_path.exists():
                    users = list_users(db_path, active_only=True)
                    admins = [u for u in users if u.role == Role.ADMIN]
                    if admins and admins[0].email != "admin@localhost":
                        username = admins[0].email
            except Exception:  # noqa: BLE001
                pass
        if not username:
            username = "admin@example.com"

    console.print("\n🍡 [bold]Provisioning Metabase Dashboard[/bold]\n")

    try:
        console.print(f"Connecting to Metabase at {url}...")
        console.print()

        # Read the known DuckDB database ID persisted by setup_metabase() at
        # first-run time, so provision_dashboard() doesn't need to guess it
        # via get_database_id()'s "DuckDB" substring search — which never
        # matches setup_metabase()'s actual f"{org_name} Analytics" naming.
        database_id = None
        try:
            import yaml

            credentials_file = project_root / ".dango" / "metabase.yml"
            if credentials_file.exists():
                stored = yaml.safe_load(credentials_file.read_text())
                database_id = (stored or {}).get("database", {}).get("id")
        except Exception:  # noqa: BLE001
            pass

        # Materialize sync history / dbt test results / source list into
        # DuckDB tables (1.0.8-DASH-1) before creating the cards, so the
        # dashboard reflects current state even on a project that has never
        # synced (empty tables, honest zero state) or whose last sync
        # predates this run of `dango dashboard provision`.
        try:
            from dango.utils.pipeline_health import materialize_pipeline_health

            materialize_pipeline_health(project_root)
        except Exception:  # noqa: BLE001
            pass

        # Refresh Metabase's own DuckDB connection so it sees the write
        # above. Metabase's embedded DuckDB connection holds a snapshot of
        # the file as of when it was opened — this is the same reason
        # dlt_runner.py calls refresh_metabase_connection() after every dbt
        # run (see that function's docstring). Without this, a Metabase
        # instance that was already running before this materialize step
        # would keep showing pre-materialization (or stale) data on the new
        # cards until something else happened to restart it.
        try:
            from dango.visualization.metabase import refresh_metabase_connection

            refresh_metabase_connection(project_root, metabase_url=url)
        except Exception:  # noqa: BLE001
            pass

        # Provision dashboard
        with console.status("[cyan]Creating dashboard...[/cyan]", spinner="dots"):
            result = provision_dashboard(
                metabase_url=url, username=username, password=password, database_id=database_id
            )

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
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e
