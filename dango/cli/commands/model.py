"""dango/cli/commands/model.py

dbt model management commands (add, remove).
"""

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
    from ..utils import check_git_branch_warning, require_project_context

    try:
        project_root = require_project_context(ctx)

        # Check git branch (gentle reminder if on main/master)
        check_git_branch_warning(project_root)

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


@model.command("remove")
@click.argument("model_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def model_remove(ctx: click.Context, model_name: str, yes: bool) -> None:
    """
    Remove a custom dbt model.

    Deletes the model SQL file. Does NOT drop the table from DuckDB -
    run 'dbt run' to rebuild without the removed model, or manually
    drop the table.

    Examples:
      dango model remove fct_daily_sales
      dango model remove int_orders --yes
    """
    from rich.prompt import Confirm

    from ..utils import require_project_context

    console.print(f"🍡 [bold]Removing Model: {model_name}[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        dbt_dir = project_root / "dbt" / "models"

        # Find model file (check intermediate and marts)
        model_file = None
        layer = None

        for layer_name in ["intermediate", "marts"]:
            potential_path = dbt_dir / layer_name / f"{model_name}.sql"
            if potential_path.exists():
                model_file = potential_path
                layer = layer_name
                break

        if not model_file:
            console.print(f"[red]Error:[/red] Model '{model_name}' not found")
            console.print("[dim]Searched in dbt/models/intermediate/ and dbt/models/marts/[/dim]")
            raise click.Abort()

        # Show model details
        console.print("[bold]Model Details:[/bold]")
        console.print(f"  Name: {model_name}")
        console.print(f"  Layer: {layer}")
        console.print(f"  File: {model_file.relative_to(project_root)}")
        console.print()

        # Check if table exists in DuckDB
        import duckdb

        duckdb_path = project_root / "data" / "warehouse.duckdb"
        table_exists = False

        if duckdb_path.exists():
            try:
                conn = duckdb.connect(str(duckdb_path))
                result = conn.execute(f"""
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = '{layer}' AND table_name = '{model_name}'
                """).fetchone()
                table_exists = result[0] > 0
                conn.close()
            except Exception:
                pass

        # Check for downstream dependencies
        downstream_models = []
        for layer_to_check in ["intermediate", "marts"]:
            layer_dir = dbt_dir / layer_to_check
            if layer_dir.exists():
                for sql_file in layer_dir.glob("*.sql"):
                    if sql_file.stem != model_name:  # Don't check self
                        try:
                            content = sql_file.read_text()
                            # Check if this file references the model being removed
                            if (
                                f"ref('{model_name}')" in content
                                or f'ref("{model_name}")' in content
                            ):
                                downstream_models.append(f"{layer_to_check}.{sql_file.stem}")
                        except Exception:
                            pass

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

        # Delete model file
        model_file.unlink()
        console.print(
            f"[green]✓[/green] Deleted model file: {model_file.relative_to(project_root)}"
        )

        # Handle table deletion if it exists
        if table_exists:
            console.print()
            if Confirm.ask(f"Also drop the table from DuckDB ({layer}.{model_name})?"):
                try:
                    conn = duckdb.connect(str(duckdb_path))
                    conn.execute(f"DROP TABLE IF EXISTS {layer}.{model_name}")
                    conn.close()
                    console.print(f"[green]✓[/green] Dropped table: {layer}.{model_name}")
                except Exception as e:
                    console.print(f"[red]✗[/red] Failed to drop table: {e}")
                    from dango.exceptions import is_debug_mode

                    if is_debug_mode():
                        import traceback

                        console.print(traceback.format_exc())
            else:
                console.print(
                    f"[yellow]⚠[/yellow]  Table {layer}.{model_name} still exists in DuckDB"
                )
                console.print(
                    "[dim]    Run 'cd dbt && dbt run' to rebuild project without this model[/dim]"
                )

        console.print()
        console.print(f"[green]✅ Model '{model_name}' removed successfully[/green]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e
