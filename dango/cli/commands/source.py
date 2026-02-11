"""dango/cli/commands/source.py

Data source management commands (add, list, remove) and sync.
"""

import click

from dango.cli import console
from dango.config.loader import (
    check_unreferenced_custom_sources,
    format_unreferenced_sources_warning,
)


@click.group()
def source() -> None:
    """
    Manage data sources.

    Commands:
      dango source add      Add a new data source
      dango source list     List all sources
      dango source remove   Remove a source
    """
    pass


@source.command("add")
@click.pass_context
def source_add(ctx: click.Context) -> None:
    """
    Add a new data source (interactive wizard).

    Supports 27+ sources across 9 categories:
      - Marketing & Analytics (7): Facebook Ads, Google Ads, Sheets, Analytics, etc.
      - Business & CRM (7): HubSpot, Salesforce, Zendesk, Jira, etc.
      - E-commerce & Payment (1): Stripe
      - Files & Storage (2): Notion, Email Inbox
      - Databases (1): MongoDB
      - Streaming (2): Kafka, Kinesis
      - Development (1): GitHub
      - Communication (1): Slack
      - Local & Custom (2): CSV, REST API
    """
    from dango.cli.source_wizard import add_source

    from ..utils import check_git_branch_warning

    project_root = ctx.obj.get("project_root")
    if not project_root:
        console.print("[red]❌ Not in a dango project directory[/red]")
        return

    # Check git branch (gentle reminder if on main/master)
    check_git_branch_warning(project_root)

    # Run wizard
    success = add_source(project_root)

    if not success:
        console.print("\n[yellow]Source not added[/yellow]")
        raise click.Abort()


@source.command("list")
@click.option("--enabled-only", is_flag=True, help="Show only enabled sources")
@click.pass_context
def source_list(ctx: click.Context, enabled_only: bool) -> None:
    """
    List all configured data sources.

    Shows source name, type, status (enabled/disabled), and last sync time.

    Examples:
      dango source list               List all sources
      dango source list --enabled-only  List only enabled sources
    """
    from datetime import datetime

    import duckdb
    from rich.table import Table

    from dango.config import get_config

    from ..utils import require_project_context

    console.print("🍡 [bold]Data Sources[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        config = get_config(project_root)

        # Check for unreferenced custom sources
        unreferenced = check_unreferenced_custom_sources(project_root, config.sources)
        if unreferenced:
            console.print(format_unreferenced_sources_warning(unreferenced))

        # Get sources
        sources = config.sources.sources

        if not sources:
            console.print("[yellow]No sources configured yet[/yellow]")
            console.print("\nRun '[cyan]dango source add[/cyan]' to add a source")
            return

        # Filter if needed
        if enabled_only:
            sources = [s for s in sources if s.enabled]
            if not sources:
                console.print("[yellow]No enabled sources found[/yellow]")
                return

        # Get last sync times from multiple sources:
        # 1. _dlt_loads table in each raw_{source_name} schema (for dlt-based sources)
        # 2. _dango_file_metadata table in main schema (for CSV sources)
        last_sync_times = {}
        duckdb_path = project_root / "data" / "warehouse.duckdb"
        if duckdb_path.exists():
            try:
                conn = duckdb.connect(str(duckdb_path), read_only=True)

                # Method 1: Check _dlt_loads tables for dlt-based sources
                # Each source has raw_{source_name}._dlt_loads with inserted_at timestamp
                for src in sources:
                    raw_schema = f"raw_{src.name}"
                    try:
                        # Check if _dlt_loads table exists for this source
                        result = conn.execute(f"""
                            SELECT MAX(inserted_at) as last_sync
                            FROM "{raw_schema}"._dlt_loads
                            WHERE status = 0
                        """).fetchone()
                        if result and result[0]:
                            # Convert to naive datetime if timezone-aware
                            last_sync_dt = result[0]
                            if hasattr(last_sync_dt, "replace") and last_sync_dt.tzinfo:
                                last_sync_dt = last_sync_dt.replace(tzinfo=None)
                            last_sync_times[src.name] = last_sync_dt
                    except Exception:
                        # Table doesn't exist for this source, continue
                        pass

                # Method 2: Check CSV metadata table (may override with more recent time)
                tables = conn.execute("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'main' AND table_name = '_dango_file_metadata'
                """).fetchall()

                if tables:
                    result = conn.execute("""
                        SELECT source_name, MAX(loaded_at) as last_sync
                        FROM _dango_file_metadata
                        WHERE status = 'loaded'
                        GROUP BY source_name
                    """).fetchall()

                    for source_name, last_sync in result:
                        # Only update if more recent than dlt load time
                        if (
                            source_name not in last_sync_times
                            or last_sync > last_sync_times[source_name]
                        ):
                            last_sync_times[source_name] = last_sync

                conn.close()
            except Exception:
                # If we can't read metadata, just skip - last_sync will show "never"
                pass

        # Create table
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Name", style="white")
        table.add_column("Type", style="dim")
        table.add_column("Status", style="white")
        table.add_column("Last Sync", style="dim")

        for src in sources:
            # Status indicator
            if src.enabled:
                status = "[green]✓ enabled[/green]"
            else:
                status = "[dim]✗ disabled[/dim]"

            # Last sync time from metadata
            if src.name in last_sync_times:
                last_sync_dt = last_sync_times[src.name]
                # Format: "2 hours ago", "3 days ago", or full date if older
                now = datetime.now()
                diff = now - last_sync_dt

                if diff.days == 0:
                    if diff.seconds < 3600:
                        minutes = diff.seconds // 60
                        last_sync = f"{minutes}m ago" if minutes > 0 else "just now"
                    else:
                        hours = diff.seconds // 3600
                        last_sync = f"{hours}h ago"
                elif diff.days == 1:
                    last_sync = "yesterday"
                elif diff.days < 7:
                    last_sync = f"{diff.days}d ago"
                else:
                    last_sync = last_sync_dt.strftime("%Y-%m-%d")
            else:
                last_sync = "[dim]never[/dim]"

            table.add_row(src.name, src.type.value, status, last_sync)

        console.print(table)
        console.print()

        # Summary
        enabled_count = sum(1 for s in config.sources.sources if s.enabled)
        total_count = len(config.sources.sources)
        console.print(
            f"[dim]Total: {total_count} sources ({enabled_count} enabled, {total_count - enabled_count} disabled)[/dim]"
        )

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort() from e


@source.command("remove")
@click.argument("source_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def source_remove(ctx: click.Context, source_name: str, yes: bool) -> None:
    """
    Remove a data source.

    SOURCE_NAME: Name of source to remove

    Examples:
      dango source remove my_csv          Remove source (with confirmation)
      dango source remove my_csv --yes    Remove without confirmation
    """
    from rich.prompt import Confirm

    from dango.config import get_config

    from ..utils import check_git_branch_warning, require_project_context

    console.print(f"🍡 [bold]Removing source: {source_name}[/bold]\n")

    try:
        project_root = require_project_context(ctx)

        # Check git branch (gentle reminder if on main/master)
        check_git_branch_warning(project_root)

        config = get_config(project_root)

        # Check if source exists
        src = config.sources.get_source(source_name)
        if not src:
            console.print(f"[red]Error:[/red] Source '{source_name}' not found")
            console.print("\nAvailable sources:")
            for s in config.sources.sources:
                console.print(f"  • {s.name} ({s.type.value})")
            raise click.Abort()

        # Show source info
        console.print("[bold]Source Details:[/bold]")
        console.print(f"  Name: {src.name}")
        console.print(f"  Type: {src.type.value}")
        console.print(f"  Status: {'enabled' if src.enabled else 'disabled'}")
        console.print()

        # Confirm deletion
        if not yes:
            console.print("[yellow]⚠️  This will remove the source configuration[/yellow]")
            console.print("[dim]Note: This does NOT delete data from DuckDB[/dim]")
            console.print("[dim]      Use 'dango db clean' afterwards to remove data[/dim]\n")

            if not Confirm.ask(f"Remove source '{source_name}'?"):
                console.print("[yellow]Cancelled[/yellow]")
                return

        # Remove from sources.yml
        sources_file = project_root / ".dango" / "sources.yml"
        if not sources_file.exists():
            console.print("[red]Error:[/red] sources.yml not found")
            raise click.Abort()

        # Read YAML
        import yaml

        with open(sources_file) as f:
            data = yaml.safe_load(f) or {}

        # Remove source
        if "sources" in data and isinstance(data["sources"], list):
            data["sources"] = [s for s in data["sources"] if s.get("name") != source_name]

            # Write back
            with open(sources_file, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)

            console.print(f"[green]✅ Source '{source_name}' removed successfully[/green]")
            console.print()
            console.print("[yellow]⚠️  Important:[/yellow]")
            console.print("  • Source configuration removed from sources.yml")
            console.print("  • [bold]Data still exists[/bold] in DuckDB tables:")
            console.print(f"    - raw.{source_name}")
            console.print(f"    - staging.{source_name}")
            console.print("  • Environment variables in .env are unchanged")
            console.print()
            console.print("[dim]To clean up orphaned tables:[/dim]")
            console.print(
                "  [cyan]dango db clean[/cyan]  # Removes tables without source config (including this one)"
            )
            console.print()
            console.print("[dim]Or to check data before cleanup:[/dim]")
            console.print(f'  [cyan]dango db query "SELECT COUNT(*) FROM raw.{source_name}"[/cyan]')
            console.print()

        else:
            console.print("[red]Error:[/red] Invalid sources.yml format")
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort() from e


@click.command()
@click.option("--source", help="Sync specific source only")
@click.option("--start-date", help="Start date for incremental loading (YYYY-MM-DD)")
@click.option("--end-date", help="End date for incremental loading (YYYY-MM-DD)")
@click.option("--full-refresh", is_flag=True, help="Drop existing data and reload from scratch")
@click.option("--dry-run", is_flag=True, help="Show what would be synced without executing")
@click.pass_context
def sync(
    ctx: click.Context,
    source: str | None,
    start_date: str | None,
    end_date: str | None,
    full_refresh: bool,
    dry_run: bool,
) -> None:
    """
    Load data from all sources (or specific source).

    Examples:
      dango sync                               Sync all enabled sources
      dango sync --source orders               Sync only 'orders' source
      dango sync --start-date 2024-01-01       Override start date
      dango sync --full-refresh                Reset state and reload all data
      dango sync --dry-run                     Preview what would be synced

    This command:
      1. Runs CSV loaders (incremental)
      2. Runs dlt pipelines (API sources)
      3. Optionally runs dbt models (transformations)
    """
    from datetime import datetime

    from dango.config import get_config
    from dango.ingestion import run_sync
    from dango.utils import DbtLock, DbtLockError

    from ..utils import require_project_context

    console.print("🍡 [bold]Syncing data...[/bold]")
    console.print()

    try:
        # Get project context
        project_root = require_project_context(ctx)

        # Try to acquire lock before running sync (which includes dbt)
        try:
            lock = DbtLock(
                project_root=project_root,
                source="cli",
                operation=f"sync {source if source else 'all sources'}",
            )
            lock.acquire()
        except DbtLockError as e:
            console.print(f"[red]Error:[/red] {str(e)}")
            raise click.Abort() from e

        # Check git branch (gentle reminder if on main/master)
        from ..utils import check_git_branch_warning

        check_git_branch_warning(project_root)

        # Load configuration
        config = get_config(project_root)

        # Check for unreferenced custom sources
        unreferenced = check_unreferenced_custom_sources(project_root, config.sources)
        if unreferenced:
            console.print(format_unreferenced_sources_warning(unreferenced))

        # Parse dates if provided
        start_date_obj = None
        end_date_obj = None

        if start_date:
            try:
                start_date_obj = datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                console.print("[red]Error:[/red] Invalid start date format. Use YYYY-MM-DD")
                raise click.Abort() from None

        if end_date:
            try:
                end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")
            except ValueError:
                console.print("[red]Error:[/red] Invalid end date format. Use YYYY-MM-DD")
                raise click.Abort() from None

        # Get sources to sync
        if source:
            # Sync specific source
            source_config = config.sources.get_source(source)
            if not source_config:
                console.print(f"[red]Error:[/red] Source '{source}' not found in sources.yml")
                console.print("\nAvailable sources:")
                for s in config.sources.sources:
                    status = "✓ enabled" if s.enabled else "✗ disabled"
                    console.print(f"  • {s.name} ({s.type.value}) - {status}")
                raise click.Abort()

            sources_to_sync = [source_config]
            console.print(f"Syncing source: [bold]{source}[/bold]")
        else:
            # Sync all enabled sources
            sources_to_sync = config.sources.get_enabled_sources()
            if not sources_to_sync:
                console.print("[yellow]No enabled sources found in sources.yml[/yellow]")
                console.print("\nRun 'dango source add' to add a source")
                return

            console.print(f"Syncing {len(sources_to_sync)} enabled source(s)")

        if full_refresh:
            console.print("[yellow]⚠️  Full refresh mode: existing data will be dropped[/yellow]")

        console.print()

        # Dry run mode - show what would be synced without executing
        if dry_run:
            console.print("[bold cyan]Dry run mode - no changes will be made[/bold cyan]\n")
            console.print("Sources that would be synced:")
            for src in sources_to_sync:
                console.print(f"  • {src.name} ({src.type.value})")
                if src.type.value == "csv":
                    console.print(f"    Path: {src.csv.file_path if src.csv else 'N/A'}")
                elif src.type.value == "dlt_native":
                    console.print(
                        f"    Module: {src.dlt_native.source_module if src.dlt_native else 'N/A'}"
                    )
                elif src.dlt_config:
                    console.print(f"    dlt source: {src.dlt_config.source_name}")

            console.print()
            console.print("[dim]Options:[/dim]")
            console.print(f"  • Full refresh: {'Yes' if full_refresh else 'No'}")
            console.print(f"  • Start date: {start_date or 'Default'}")
            console.print(f"  • End date: {end_date or 'Default'}")
            console.print()
            console.print("[dim]Run without --dry-run to execute sync[/dim]")
            return

        # Run sync
        summary = run_sync(
            project_root=project_root,
            sources=sources_to_sync,
            start_date=start_date_obj,
            end_date=end_date_obj,
            full_refresh=full_refresh,
        )

        # Trigger Metabase schema sync (if Metabase is running)
        if summary["failed_count"] == 0:
            console.print()
            console.print("[dim]Updating Metabase schema...[/dim]")
            from dango.visualization.metabase import sync_metabase_schema

            if sync_metabase_schema(project_root):
                console.print("[green]✓[/green] Metabase schema updated")
            else:
                # Silent skip if Metabase isn't configured or running
                console.print(
                    "[dim]ℹ Metabase not running (schema will sync automatically when started)[/dim]"
                )

        # Display OAuth warnings at the very end (so users don't miss them)
        oauth_warnings = summary.get("oauth_warnings", [])
        if oauth_warnings:
            console.print()
            console.print("[yellow]" + "=" * 60 + "[/yellow]")
            console.print("[yellow]⚠️  OAuth Token Warnings:[/yellow]")
            console.print("[yellow]" + "=" * 60 + "[/yellow]")
            for warning in oauth_warnings:
                console.print(
                    f"  • {warning['source_name']}: expires in {warning['days_left']} day(s) ({warning['expires_at']})"
                )
                console.print(
                    f"    [cyan]Re-authenticate:[/cyan] dango auth {warning['source_type']}"
                )
            console.print()

        # Exit with error code if any sources failed
        if summary["failed_count"] > 0:
            lock.release()
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        lock.release()
        raise click.Abort() from e
    finally:
        # Always release the lock (if it was acquired)
        try:
            lock.release()
        except Exception:
            pass
