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


_VALID_MODEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_VALID_LAYERS = ("intermediate", "marts")


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
    import duckdb
    from rich.prompt import Confirm

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
        dbt_dir = project_root / "dbt" / "models"

        # Find model file (check intermediate and marts)
        model_file = None
        layer = None

        for layer_name in _VALID_LAYERS:
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

        # Check if table exists in DuckDB (read-only connection)
        duckdb_path = project_root / "data" / "warehouse.duckdb"
        table_exists = False

        if duckdb_path.exists():
            try:
                conn = duckdb.connect(str(duckdb_path), config={"access_mode": "read_only"})
                result = conn.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = ? AND table_name = ?
                    """,
                    [layer, model_name],
                ).fetchone()
                table_exists = (result[0] > 0) if result else False
                conn.close()
            except Exception:
                pass

        # Check for downstream dependencies
        downstream_models = []
        for layer_to_check in _VALID_LAYERS:
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

        # Check monitors.yml for references
        monitor_refs: list[str] = []
        try:
            from dango.analysis.config import load_monitors_config

            monitors_cfg = load_monitors_config(project_root)
            for m in monitors_cfg.monitors:
                # source_table is "layer.table_name", check if model_name matches
                if m.source_table.endswith(f".{model_name}"):
                    monitor_refs.append(m.name)
        except Exception:
            pass

        # Dry run: show what would be removed and exit
        if dry_run:
            if layer is None:
                raise click.Abort()  # unreachable — defensive guard
            console.print("[bold cyan]Dry run — no changes will be made[/bold cyan]\n")
            console.print("[bold]Would remove:[/bold]")
            console.print(f"  • Model file: {model_file.relative_to(project_root)}")
            schema_path = dbt_dir / layer / "schema.yml"
            if schema_path.exists():
                console.print(f"  • schema.yml entry in {schema_path.relative_to(project_root)}")
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

        # Delete model file
        model_file.unlink()
        console.print(
            f"[green]✓[/green] Deleted model file: {model_file.relative_to(project_root)}"
        )

        # Remove model entry from schema.yml (layer guaranteed set by model_file check)
        if layer is None:
            raise click.Abort()  # unreachable — defensive guard
        schema_path = dbt_dir / layer / "schema.yml"
        if schema_path.exists():
            import yaml

            try:
                with open(schema_path) as f:
                    schema_data = yaml.safe_load(f) or {}
                if "models" in schema_data and isinstance(schema_data["models"], list):
                    before = len(schema_data["models"])
                    schema_data["models"] = [
                        m for m in schema_data["models"] if m.get("name") != model_name
                    ]
                    if len(schema_data["models"]) < before:
                        if schema_data["models"]:
                            with open(schema_path, "w") as f:
                                yaml.dump(
                                    schema_data,
                                    f,
                                    default_flow_style=False,
                                    sort_keys=False,
                                )
                        else:
                            schema_path.unlink()
                        console.print(
                            f"[green]✓[/green] Removed from {schema_path.relative_to(project_root)}"
                        )
            except Exception:
                pass  # Non-critical — don't block removal

        # Remove monitor references from monitors.yml
        if monitor_refs:
            try:
                from dango.analysis.config import load_monitors_config, save_monitors_config

                monitors_cfg = load_monitors_config(project_root)
                original_count = len(monitors_cfg.monitors)
                monitors_cfg.monitors = [
                    m for m in monitors_cfg.monitors if m.name not in monitor_refs
                ]
                if len(monitors_cfg.monitors) < original_count:
                    save_monitors_config(project_root, monitors_cfg)
                    removed_count = original_count - len(monitors_cfg.monitors)
                    console.print(
                        f"[green]✓[/green] Removed {removed_count} monitor(s) from monitors.yml"
                    )
            except Exception:
                pass  # Non-critical

        # Handle table deletion if it exists
        dropped_table = False
        if table_exists:
            console.print()
            if yes or Confirm.ask(f"Also drop the table from DuckDB ({layer}.{model_name})?"):
                try:
                    # layer and model_name are validated: layer from _VALID_LAYERS,
                    # model_name matches _VALID_MODEL_NAME_RE — safe to interpolate
                    conn = duckdb.connect(str(duckdb_path))
                    conn.execute(f'DROP TABLE IF EXISTS "{layer}"."{model_name}"')
                    conn.close()
                    console.print(f"[green]✓[/green] Dropped table: {layer}.{model_name}")
                    dropped_table = True
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

        # Refresh Metabase schema if table was dropped
        if dropped_table:
            try:
                from dango.visualization.metabase import sync_metabase_schema

                if sync_metabase_schema(project_root):
                    console.print("[green]✓[/green] Metabase schema refreshed")
            except Exception:
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
