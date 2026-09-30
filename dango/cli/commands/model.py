"""dango/cli/commands/model.py

dbt model management commands (add, remove).
"""

import re

import click

from dango.cli import console


@click.group()
@click.pass_context
def model(ctx: click.Context) -> None:
    """
    Manage dbt models.

    Commands:
      dango model add    Create a new intermediate or marts model
    """
    pass


@model.command("add")
@click.pass_context
def model_add(ctx: click.Context) -> None:
    """
    Create a new dbt model (intermediate or marts layer).

    This interactive wizard helps you create:
    - Intermediate models: Reusable business logic
    - Marts models: Final business metrics

    Staging models are auto-generated during 'dango sync',
    so this wizard only handles intermediate and marts layers.

    Examples:
      dango model add    Run interactive wizard
    """
    from ..model_wizard import add_model
    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)

        model_path = add_model(project_root)

        if not model_path:
            raise click.Abort()

    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled[/yellow]")
        raise click.Abort() from None
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


_VALID_MODEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@model.command("remove")
@click.argument("model_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without executing")
@click.pass_context
def model_remove(ctx: click.Context, model_name: str, yes: bool, dry_run: bool) -> None:
    """
    Remove a custom dbt model and cascade cleanup.

    Removes the model SQL file, schema.yml entry, monitors.yml references,
    and optionally drops the DuckDB table and refreshes Metabase schema.

    Examples:
      dango model remove fct_daily_sales
      dango model remove int_orders --yes
      dango model remove fct_daily_sales --dry-run
    """
    from rich.prompt import Confirm

    from dango.transformation.model_service import ModelServiceError, remove_model

    from ..utils import require_project_context

    console.print(f"🍡 [bold]Removing Model: {model_name}[/bold]\n")

    # Validate model_name to prevent injection in SQL/filesystem
    if not _VALID_MODEL_NAME_RE.match(model_name):
        console.print(
            "[red]Error:[/red] Invalid model name. "
            "Must be lowercase, start with a letter, and contain only letters, digits, underscores."
        )
        raise click.Abort()

    try:
        project_root = require_project_context(ctx)

        # Gather everything first (dry run writes nothing)
        try:
            preview = remove_model(project_root, model_name, dry_run=True)
        except ModelServiceError as e:
            for err in e.errors:
                console.print(f"[red]Error:[/red] {err}")
            raise click.Abort() from e
        layer = preview.layer
        table_exists = preview.table_existed
        downstream_models = preview.downstream
        monitor_refs = preview.monitors_removed

        # Show model details
        console.print("[bold]Model Details:[/bold]")
        console.print(f"  Name: {model_name}")
        console.print(f"  Layer: {layer}")
        console.print(f"  File: {preview.path}")
        console.print()

        # Dry run: show what would be removed and exit
        if dry_run:
            console.print("[bold cyan]Dry run — no changes will be made[/bold cyan]\n")
            console.print("[bold]Would remove:[/bold]")
            console.print(f"  • Model file: {preview.path}")
            for changed in preview.files_changed[1:]:
                if changed.endswith("monitors.yml"):
                    continue
                console.print(f"  • schema.yml entry in {changed}")
            if monitor_refs:
                console.print(f"  • Monitor references: {', '.join(monitor_refs)}")
            if table_exists:
                console.print(f"  • DuckDB table: {layer}.{model_name}")
            if downstream_models:
                console.print("\n[yellow]⚠️  Downstream models that would break:[/yellow]")
                for dep in downstream_models:
                    console.print(f"    • {dep}")
            console.print()
            return

        # Warn about dependencies
        if downstream_models:
            console.print("[red]⚠️  WARNING: Other models depend on this model![/red]")
            console.print("\n[bold]Downstream dependencies:[/bold]")
            for dep in downstream_models:
                console.print(f"  • {dep}")
            console.print()
            console.print("[yellow]Removing this model will break downstream models.[/yellow]")
            console.print(
                "[dim]Consider removing downstream models first, or updating them.[/dim]\n"
            )

            if not yes:
                if not Confirm.ask(f"Continue removing '{model_name}' anyway?"):
                    console.print("[yellow]Cancelled[/yellow]")
                    return

        # Confirm deletion
        if not yes and not downstream_models:  # Skip if already confirmed above
            console.print("[yellow]⚠️  This will delete the model file[/yellow]")
            if table_exists:
                console.print(
                    f"[dim]The table {layer}.{model_name} exists and will be offered for removal[/dim]\n"
                )
            else:
                console.print(
                    "[dim]No table found in DuckDB (model may not have been run yet)[/dim]\n"
                )

            if not Confirm.ask(f"Remove model '{model_name}'?"):
                console.print("[yellow]Cancelled[/yellow]")
                return

        # Ask every question before acting
        drop_table = False
        if table_exists:
            console.print()
            drop_table = yes or Confirm.ask(
                f"Also drop the table from DuckDB ({layer}.{model_name})?"
            )

        try:
            result = remove_model(
                project_root,
                model_name,
                drop_table=drop_table,
                force=True,
                lock_source="cli",
            )
        except ModelServiceError as e:
            for err in e.errors:
                console.print(f"[red]Error:[/red] {err}")
            raise click.Abort() from e

        console.print(f"[green]✓[/green] Deleted model file: {result.path}")
        for changed in result.files_changed[1:]:
            if changed.endswith("monitors.yml"):
                console.print(
                    f"[green]✓[/green] Removed {len(result.monitors_removed)} monitor(s) from monitors.yml"
                )
            else:
                console.print(f"[green]✓[/green] Removed from {changed}")
        for warning in result.warnings:
            console.print(f"[red]✗[/red] {warning}")

        if result.dropped_table:
            console.print(f"[green]✓[/green] Dropped table: {layer}.{model_name}")
        elif table_exists:
            console.print(f"[yellow]⚠[/yellow]  Table {layer}.{model_name} still exists in DuckDB")
            console.print(
                "[dim]    Run 'cd dbt && dbt run' to rebuild project without this model[/dim]"
            )

        # Refresh Metabase schema if table was dropped
        if result.dropped_table:
            try:
                from dango.visualization.metabase import (
                    refresh_metabase_connection,
                    sync_metabase_schema,
                )

                mb_ok, _mb_err, mb_session_id = refresh_metabase_connection(project_root)
                if mb_ok and sync_metabase_schema(project_root, existing_session_id=mb_session_id):
                    console.print("[green]✓[/green] Metabase schema refreshed")
            except Exception:  # noqa: BLE001
                pass  # Non-critical — Metabase may not be running

        console.print()
        console.print(f"[green]✅ Model '{model_name}' removed successfully[/green]")

    except click.Abort:
        raise
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e
