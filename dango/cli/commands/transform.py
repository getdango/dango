"""dango/cli/commands/transform.py

dbt transformation commands (run, docs, generate).
"""

import os
from typing import Any

import click

from dango.cli import console


@click.command(
    "run", context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False}
)
@click.argument("dbt_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def run(ctx: click.Context, dbt_args: tuple[str, ...]) -> None:
    """
    Run dbt models (wrapper for dbt run).

    This command works from anywhere within your project directory
    and automatically finds the dbt project.

    Examples:
      dango run                           Run all models
      dango run --select my_model         Run specific model
      dango run --select my_model+        Run model and downstream
      dango run --select tag:marts        Run models with tag
      dango run --full-refresh            Full refresh of incremental models

    Any dbt run arguments are passed through to dbt.
    See 'dbt run --help' for all available options.
    """
    import subprocess

    from dango.utils import DbtLock, DbtLockError
    from dango.utils.dbt_status import update_model_status

    from ..utils import require_project_context

    lock = None
    _cloud_mode = False
    try:
        project_root = require_project_context(ctx)
        dbt_dir = project_root / "dbt"

        if not dbt_dir.exists():
            console.print("[red]Error:[/red] dbt directory not found")
            console.print(f"[dim]Expected: {dbt_dir}[/dim]")
            raise click.Abort()

        # Build dbt command
        cmd = ["dbt", "build", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)]
        if dbt_args:
            cmd.extend(dbt_args)

        # Try to acquire lock before running dbt
        try:
            lock = DbtLock(
                project_root=project_root,
                source="cli",
                operation=f"dbt build {' '.join(dbt_args) if dbt_args else ''}",
            )
            lock.acquire()
        except DbtLockError as e:
            console.print(f"[red]Error:[/red] {str(e)}")
            raise click.Abort() from e

        console.print(f"[dim]Running: {' '.join(cmd)}[/dim]\n")

        # Stop Metabase on cloud to prevent DuckDB lock conflicts
        _cloud_mode = os.environ.get("DANGO_CLOUD_MODE") == "true"
        if _cloud_mode:
            try:
                import subprocess as _sp

                from dango.platform.docker import get_compose_project_name

                _proj_name = get_compose_project_name(project_root)
                _env = {**os.environ, "COMPOSE_PROJECT_NAME": _proj_name}
                _sp.run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(project_root / "docker-compose.yml"),
                        "stop",
                        "metabase",
                    ],
                    capture_output=True,
                    timeout=60,
                    env=_env,
                )
                import time

                time.sleep(3)
            except Exception:
                console.print("[dim]ℹ Could not pause Metabase (continuing anyway)[/dim]")

        # Run dbt command from dbt directory for correct path resolution
        result = subprocess.run(cmd, cwd=str(dbt_dir))

        if result.returncode != 0:
            console.print(f"\n[red]dbt build failed with exit code {result.returncode}[/red]")
            raise click.Abort()

        # Update persistent model status
        update_model_status(project_root)

        # Update schema.yml files for intermediate/marts models
        console.print("\n[dim]Updating schema.yml files...[/dim]")
        from dango.cli.schema_manager import update_model_schemas

        # Get list of all intermediate/marts models
        models_to_update = []
        for layer in ["intermediate", "marts"]:
            layer_dir = dbt_dir / "models" / layer
            if layer_dir.exists():
                for sql_file in layer_dir.glob("*.sql"):
                    if not sql_file.name.startswith("_"):
                        models_to_update.append(sql_file.stem)

        if models_to_update:
            update_model_schemas(project_root, models_to_update)

        # Refresh Metabase connection to see new/updated tables.
        # Skip on cloud — Metabase is stopped; the finally block restarts it
        # and Metabase auto-syncs schema on startup.
        if not _cloud_mode:
            console.print("\n[dim]Refreshing Metabase connection...[/dim]")
            from dango.visualization.metabase import (
                refresh_metabase_connection,
                sync_metabase_schema,
            )

            if refresh_metabase_connection(project_root):
                console.print("[green]✓ Metabase connection refreshed[/green]")
                # Also sync schema to discover new tables/schemas from dbt run
                if sync_metabase_schema(project_root):
                    console.print("[green]✓ Metabase schema synced[/green]")
            else:
                console.print("[dim]ℹ Metabase not running (will sync when started)[/dim]")

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
    finally:
        if lock is not None and lock._acquired:
            try:
                lock.release()
            except Exception:
                pass
        # Restart Metabase on cloud
        if _cloud_mode:
            try:
                import subprocess as _sp

                from dango.platform.docker import get_compose_project_name

                _proj_name = get_compose_project_name(project_root)
                _env = {**os.environ, "COMPOSE_PROJECT_NAME": _proj_name}
                _sp.run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(project_root / "docker-compose.yml"),
                        "start",
                        "metabase",
                    ],
                    capture_output=True,
                    timeout=120,
                    env=_env,
                )
            except Exception:
                console.print(
                    "[yellow]Warning: Could not restart Metabase — "
                    "run 'docker compose start metabase' manually[/yellow]"
                )


@click.command("docs")
@click.pass_context
def docs(ctx: click.Context) -> None:
    """
    Generate dbt documentation (wrapper for dbt docs generate).

    This command generates documentation for your dbt models, sources, and tests.
    After generation, view docs at http://localhost:{port}/catalog (if platform is running).

    Examples:
      dango docs          Generate documentation
      dango start         Then view at http://localhost:{port}/catalog
    """
    import subprocess

    from dango.config import ConfigLoader

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)
        dbt_dir = project_root / "dbt"

        if not dbt_dir.exists():
            console.print("[red]Error:[/red] dbt directory not found")
            console.print(f"[dim]Expected: {dbt_dir}[/dim]")
            raise click.Abort()

        console.print("[dim]Generating dbt documentation...[/dim]\n")

        # Build dbt command
        cmd = [
            "dbt",
            "docs",
            "generate",
            "--project-dir",
            str(dbt_dir),
            "--profiles-dir",
            str(dbt_dir),
        ]

        # Run dbt command from dbt directory for correct path resolution
        result = subprocess.run(cmd, cwd=str(dbt_dir))

        if result.returncode != 0:
            console.print(
                f"\n[red]dbt docs generate failed with exit code {result.returncode}[/red]"
            )
            raise click.Abort()

        console.print()
        console.print("[green]✓ Documentation generated successfully[/green]")
        console.print()

        # Get platform port from config for proxy URL
        config_loader = ConfigLoader(project_root)
        config = config_loader.load_config()
        platform_port = config.platform.port
        dbt_docs_url = f"http://localhost:{platform_port}/catalog"

        # Check if platform is running
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        platform_running = sock.connect_ex(("127.0.0.1", platform_port)) == 0
        sock.close()

        if platform_running:
            console.print(f"[bold]View documentation:[/bold] [cyan]{dbt_docs_url}[/cyan]")
        else:
            console.print("[dim]To view documentation:[/dim]")
            console.print("  1. Start platform: [cyan]dango start[/cyan]")
            console.print(f"  2. Open browser: [cyan]{dbt_docs_url}[/cyan]")

        console.print()

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


@click.command("generate")
@click.option("--models", is_flag=True, help="Generate dbt staging models")
@click.option(
    "--all", "generate_all", is_flag=True, help="Generate all dbt artifacts (models + schema)"
)
@click.pass_context
def generate(ctx: click.Context, models: bool, generate_all: bool) -> None:
    """
    Generate dbt models and artifacts from data sources.

    This command introspects your DuckDB warehouse and automatically generates:
    - Staging models (stg_*.sql) with deduplication logic
    - Schema definitions (schema.yml) with tests and documentation

    Examples:
      dango generate --models      Generate staging models only
      dango generate --all         Generate models + schema.yml

    Note: Run 'dango sync' first to load data into DuckDB
    """
    from rich.table import Table

    from dango.config import get_config
    from dango.transformation.generator import DbtModelGenerator

    from ..utils import require_project_context

    console.print("🍡 [bold]Generating dbt Models[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        config = get_config(project_root)

        # Get enabled sources
        sources = config.sources.get_enabled_sources()

        if not sources:
            console.print("[yellow]No enabled sources found[/yellow]")
            console.print("\nRun 'dango source add' to add sources")
            return

        # Default to --all if no flag specified
        if not models and not generate_all:
            generate_all = True

        console.print(f"Generating models for {len(sources)} source(s)...")
        console.print()

        # Initialize generator
        generator = DbtModelGenerator(project_root)

        # Generate models (force regenerate all in manual generate command)
        summary = generator.generate_all_models(
            sources=sources,
            generate_schema_yml=generate_all,
            skip_customized=False,  # Manual command always regenerates
        )

        # Display results
        generated_items: list[dict[str, Any]] = summary.get("generated") or []
        if generated_items:
            console.print("[green]✅ Generated Models:[/green]\n")

            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("Source", style="white")
            table.add_column("Columns", style="dim")
            table.add_column("Dedup Strategy", style="cyan")
            table.add_column("Files", style="dim")

            for item in generated_items:
                source_name = item["source"]
                item_models = item.get("models", [])

                # For each model generated for this source
                for model in item_models:
                    files = "model"
                    if item.get("schema"):
                        files += " + schema"

                    table.add_row(
                        f"{source_name} ({model['endpoint']})",
                        str(model.get("columns", "N/A")),
                        model.get("dedup_strategy", "N/A"),
                        files,
                    )

            console.print(table)
            console.print()

        if summary["skipped"]:
            console.print("[yellow]⚠️  Skipped:[/yellow]\n")
            for item in summary["skipped"]:
                console.print(f"  • {item['source']}: {item['reason']}")
            console.print()

        if summary["errors"]:
            console.print("[red]❌ Errors:[/red]\n")
            for item in summary["errors"]:
                console.print(f"  • {item['source']}: {item['error']}")
            console.print()

        # Summary
        console.print("[bold]Summary:[/bold]")
        console.print(f"  Generated: {len(summary['generated'])}")
        console.print(f"  Skipped: {len(summary['skipped'])}")
        console.print(f"  Errors: {len(summary['errors'])}")

        # Next steps
        if summary["generated"]:
            console.print()
            console.print("[cyan]Next steps:[/cyan]")
            console.print("  1. Review generated models in dbt/models/staging/")
            console.print("  2. Run: [bold]cd dbt && dbt run[/bold]")
            console.print("  3. Run: [bold]dbt test[/bold]")
            console.print("  4. View docs: [bold]dbt docs generate && dbt docs serve[/bold]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e
