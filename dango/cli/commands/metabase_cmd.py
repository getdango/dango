"""dango/cli/commands/metabase_cmd.py

Metabase asset management commands (save, load, refresh).
"""

import click

from dango.cli import console


@click.group()
@click.pass_context
def metabase(ctx: click.Context) -> None:
    """
    Manage Metabase assets (dashboards, questions).

    Commands:
      dango metabase save     Export dashboards/questions to files
      dango metabase load     Import dashboards/questions from files
    """
    pass


@metabase.command("save")
@click.option(
    "--all",
    "include_personal",
    is_flag=True,
    help="Include personal collections (default: only shared/team)",
)
@click.option("--collections", help="Specific collections to export (comma-separated)")
@click.pass_context
def metabase_save(ctx: click.Context, include_personal: bool, collections: str | None) -> None:
    """
    Save Metabase dashboards and questions to files.

    Exports to metabase/ directory in YAML format.
    By default, excludes personal collections (only exports shared/team assets).

    What this does:
    - Exports dashboards from Metabase to metabase/dashboards/ (YAML files)
    - Exports questions from Metabase to metabase/questions/ (YAML files)
    - Excludes personal collections by default
    - Files can optionally be committed to git for version control

    Workflow:
      1. Make changes in Metabase UI
      2. Run 'dango metabase save'
      3. (Optional) Commit to git: git add metabase/ && git commit -m "Update dashboards"

    Examples:
      dango metabase save                          # Export shared/team collections
      dango metabase save --all                    # Include personal collections
      dango metabase save --collections "Shared,Marketing"  # Specific collections
    """
    from dango.visualization.dashboard_manager import DashboardManager

    from ..utils import require_project_context

    console.print("\n🍡 [bold]Saving Metabase Assets[/bold]\n")

    try:
        project_root = require_project_context(ctx)

        # Parse collections if provided
        collection_list = None
        if collections:
            collection_list = [c.strip() for c in collections.split(",")]

        # Create dashboard manager
        manager = DashboardManager(project_root)

        with console.status("[cyan]Exporting dashboards and questions...[/cyan]", spinner="dots"):
            result = manager.save_to_files(
                include_personal=include_personal, collections=collection_list
            )

        if result["success"]:
            total = (
                len(result["exported_dashboards"])
                + len(result["exported_questions"])
                + len(result.get("exported_models", []))
                + len(result.get("exported_metrics", []))
                + len(result.get("exported_timelines", []))
            )
            console.print(f"[green]✅ Exported {total} item(s) to metabase/[/green]\n")

            if result["exported_dashboards"]:
                console.print("[bold]Dashboards:[/bold]")
                for item in result["exported_dashboards"]:
                    console.print(
                        f"  ✓ {item['name']} ({item['cards']} cards) - {item['collection']}"
                    )
                console.print()

            if result["exported_questions"]:
                console.print("[bold]Questions:[/bold]")
                for item in result["exported_questions"]:
                    console.print(f"  ✓ {item['name']} ({item['type']}) - {item['collection']}")
                console.print()

            if result.get("exported_models"):
                console.print("[bold]Models:[/bold]")
                for item in result["exported_models"]:
                    console.print(f"  ✓ {item['name']} ({item['type']}) - {item['collection']}")
                console.print()

            if result.get("exported_metrics"):
                console.print("[bold]Metrics:[/bold]")
                for item in result["exported_metrics"]:
                    console.print(f"  ✓ {item['name']} - {item['collection']}")
                console.print()

            if result.get("exported_timelines"):
                console.print("[bold]Timelines:[/bold]")
                for item in result["exported_timelines"]:
                    console.print(f"  ✓ {item['name']} - {item['collection']}")
                console.print()

            if result["skipped_collections"]:
                console.print(
                    f"[dim]⏭  Skipped {len(result['skipped_collections'])} personal collection(s)[/dim]"
                )
                for name in result["skipped_collections"]:
                    console.print(f"[dim]    • {name}[/dim]")
                console.print()

            console.print("[bold cyan]Next steps:[/bold cyan]")
            console.print("  • Files saved to metabase/ directory")
            console.print("  • (Optional) Commit to git for version control:")
            console.print('    [dim]git add metabase/ && git commit -m "Update dashboards"[/dim]')

        else:
            console.print("[yellow]⚠️  No assets exported[/yellow]\n")
            if result["errors"]:
                for error in result["errors"]:
                    console.print(f"  [red]•[/red] {error}")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@metabase.command("load")
@click.option(
    "--overwrite",
    is_flag=True,
    help="Overwrite existing dashboards/questions (WARNING: destructive)",
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be imported without actually importing"
)
@click.pass_context
def metabase_load(ctx: click.Context, overwrite: bool, dry_run: bool) -> None:
    """
    Load Metabase dashboards and questions from files.

    Imports from metabase/ directory into Metabase.
    By default, skips existing items. Use --overwrite to replace existing.

    Behavior:
    - Default: Skip dashboards that already exist in Metabase (safe)
    - --overwrite: Replace existing dashboards with file versions (destructive)
    - --dry-run: Preview what would be imported without making changes

    WARNING: --overwrite will replace existing dashboards/questions in Metabase
             with versions from files. Uncommitted Metabase changes will be lost!

    Workflow:
      1. (If using git) Pull changes: git pull
      2. Import: dango metabase load
      3. Dashboards from files are now in Metabase

    Examples:
      dango metabase load                   # Import new items, skip existing
      dango metabase load --dry-run         # Preview what would be imported
      dango metabase load --overwrite       # Replace existing items (destructive)
    """
    from dango.visualization.dashboard_manager import DashboardManager

    from ..utils import require_project_context

    console.print("\n🍡 [bold]Loading Metabase Assets[/bold]\n")

    try:
        project_root = require_project_context(ctx)

        # Warning for overwrite mode
        if overwrite and not dry_run:
            console.print("[bold yellow]⚠️  WARNING: Overwrite Mode[/bold yellow]")
            console.print("This will replace existing dashboards/questions in Metabase")
            console.print("with versions from files. Unsaved Metabase changes will be lost!\n")

            if not click.confirm("Continue?"):
                console.print("[yellow]Cancelled[/yellow]")
                return

        # Create dashboard manager
        manager = DashboardManager(project_root)

        mode_str = (
            "[dim](dry-run)[/dim]"
            if dry_run
            else "[dim](overwrite)[/dim]"
            if overwrite
            else "[dim](skip existing)[/dim]"
        )
        with console.status(f"[cyan]Loading assets {mode_str}...[/cyan]", spinner="dots"):
            result = manager.load_from_files(overwrite=overwrite, dry_run=dry_run)

        if result["success"]:
            if dry_run:
                console.print("[bold cyan]Preview (dry-run mode):[/bold cyan]\n")

            total_imported = (
                len(result["imported_dashboards"])
                + len(result["imported_questions"])
                + len(result.get("imported_models", []))
                + len(result.get("imported_metrics", []))
                + len(result.get("imported_timelines", []))
            )
            total_skipped = len(result["skipped"])

            if total_imported > 0:
                console.print(
                    f"[green]✅ {'Would import' if dry_run else 'Imported'} {total_imported} item(s)[/green]\n"
                )

                if result["imported_dashboards"]:
                    console.print("[bold]Dashboards:[/bold]")
                    for item in result["imported_dashboards"]:
                        status_icon = "?" if dry_run else "✓"
                        console.print(f"  {status_icon} {item['name']} ({item['cards']} cards)")
                    console.print()

                if result["imported_questions"]:
                    console.print("[bold]Questions:[/bold]")
                    for item in result["imported_questions"]:
                        status_icon = "?" if dry_run else "✓"
                        console.print(f"  {status_icon} {item['name']}")
                    console.print()

                if result.get("imported_models"):
                    console.print("[bold]Models:[/bold]")
                    for item in result["imported_models"]:
                        status_icon = "?" if dry_run else "✓"
                        console.print(f"  {status_icon} {item['name']}")
                    console.print()

                if result.get("imported_metrics"):
                    console.print("[bold]Metrics:[/bold]")
                    for item in result["imported_metrics"]:
                        status_icon = "?" if dry_run else "✓"
                        console.print(f"  {status_icon} {item['name']}")
                    console.print()

                if result.get("imported_timelines"):
                    console.print("[bold]Timelines:[/bold]")
                    for item in result["imported_timelines"]:
                        status_icon = "?" if dry_run else "✓"
                        console.print(f"  {status_icon} {item['name']}")
                    console.print()

            if total_skipped > 0:
                console.print(f"[dim]⏭  Skipped {total_skipped} existing item(s)[/dim]")
                for item in result["skipped"]:
                    console.print(
                        f"[dim]    • {item['name']} ({item['type']}) - {item['reason']}[/dim]"
                    )
                console.print()

            if result["would_overwrite"] and (overwrite or dry_run):
                console.print(
                    f"[yellow]⚠️  {'Would overwrite' if dry_run else 'Overwrote'} {len(result['would_overwrite'])} existing item(s)[/yellow]"
                )
                for item in result["would_overwrite"]:
                    console.print(f"[yellow]    • {item['name']} ({item['type']})[/yellow]")
                console.print()

            if dry_run and total_imported > 0:
                console.print("[bold cyan]To actually import:[/bold cyan]")
                console.print("  dango metabase load")

        else:
            console.print("[yellow]⚠️  Nothing to import[/yellow]\n")
            if result["errors"]:
                for error in result["errors"]:
                    console.print(f"  [red]•[/red] {error}")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@metabase.command("refresh")
@click.pass_context
def metabase_refresh(ctx: click.Context) -> None:
    """
    Refresh Metabase schema to discover new tables and schemas.

    Use this when you've created new schemas (e.g., marts) and want
    Metabase to discover them. This triggers a non-destructive schema
    sync that preserves all existing questions and dashboards.

    Examples:
      dango metabase refresh    # Refresh to discover new schemas
    """
    import requests
    import yaml

    from dango.visualization.metabase import sync_metabase_schema

    from ..utils import require_project_context

    console.print("\n🍡 [bold]Refreshing Metabase Schema[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        credentials_file = project_root / ".dango" / "metabase.yml"

        if not credentials_file.exists():
            console.print("[red]✗[/red] Metabase not configured. Run 'dango start' first.")
            raise click.Abort()

        # Load credentials for health check and display
        with open(credentials_file) as f:
            credentials = yaml.safe_load(f)

        metabase_url = credentials.get("metabase_url", "http://localhost:3000")
        db_id = credentials.get("database", {}).get("id")

        # Check if Metabase is running
        try:
            health_response = requests.get(f"{metabase_url}/api/health", timeout=2)
            if health_response.status_code != 200:
                console.print("[red]✗[/red] Metabase is not running. Start with 'dango start'.")
                raise click.Abort()
        except requests.exceptions.RequestException:
            console.print("[red]✗[/red] Cannot connect to Metabase. Start with 'dango start'.")
            raise click.Abort() from None

        console.print("[green]✓[/green] Metabase is running")

        # Trigger non-destructive schema sync (handles login, sync, polling,
        # and visibility rules internally)
        console.print("[cyan]Syncing schema...[/cyan]")
        success = sync_metabase_schema(project_root, metabase_url)

        if not success:
            console.print("[red]✗[/red] Schema sync failed. Check Metabase logs for details.")
            raise click.Abort()

        console.print("[green]✓[/green] Schema sync complete")

        # Show discovered schemas/tables for user feedback
        if db_id:
            admin = credentials.get("admin", {})
            login_response = requests.post(
                f"{metabase_url}/api/session",
                json={"username": admin.get("email"), "password": admin.get("password")},
                timeout=10,
            )

            if login_response.status_code == 200:
                session_id = login_response.json().get("id")
                headers = {"X-Metabase-Session": session_id}

                metadata_response = requests.get(
                    f"{metabase_url}/api/database/{db_id}/metadata",
                    headers=headers,
                    timeout=10,
                )

                if metadata_response.status_code == 200:
                    tables = metadata_response.json().get("tables", [])
                    schemas = {t.get("schema") for t in tables}

                    console.print(
                        f"\n[bold]Discovered schemas:[/bold] {', '.join(sorted(schemas))}"
                    )
                    console.print(f"[bold]Total tables:[/bold] {len(tables)}\n")

                    for schema in sorted(schemas):
                        schema_tables = [t.get("name") for t in tables if t.get("schema") == schema]
                        console.print(f"  [cyan]{schema}[/cyan]: {', '.join(schema_tables)}")

        console.print("\n[green]✨ Metabase schema refreshed successfully![/green]\n")

    except click.Abort:
        raise
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e
