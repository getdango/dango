"""dango/cli/commands/config_cmd.py

Configuration management commands (validate, show).
"""

import click

from dango.cli import console


@click.group()
def config() -> None:
    """
    Manage Dango configuration.

    Commands:
      dango config validate   Validate configuration files
      dango config show       Show current configuration
    """
    pass


@config.command("validate")
@click.pass_context
def config_validate(ctx: click.Context) -> None:
    """
    Validate all configuration files:
    - .dango/sources.yml (source configuration)
    - .dango/project.yml (project settings)
    - dbt/models/staging/*/sources.yml (dbt source documentation)
    """
    import yaml

    from dango.config import ConfigLoader

    from ..utils import require_project_context

    console.print("🍡 [bold]Validating configuration files[/bold]")
    console.print()

    try:
        project_root = require_project_context(ctx)
        loader = ConfigLoader(project_root)

        all_valid = True

        # 1. Validate main config files
        console.print("[cyan]Checking .dango/sources.yml and project.yml...[/cyan]")
        is_valid, errors = loader.validate_config()

        if is_valid:
            console.print("[green]✓[/green] Main config files valid")
        else:
            all_valid = False
            console.print("[red]✗[/red] Main config has errors:")
            for error in errors:
                console.print(f"    • {error}")

        console.print()

        # 2. Validate dbt sources.yml files
        console.print("[cyan]Checking dbt sources.yml files...[/cyan]")
        staging_dir = project_root / "dbt" / "models" / "staging"

        if not staging_dir.exists():
            console.print("[dim]  No staging models yet (run sync first)[/dim]")
        else:
            sources_files = list(staging_dir.glob("*/sources.yml"))

            if not sources_files:
                console.print("[dim]  No sources.yml files yet (run sync first)[/dim]")
            else:
                dbt_errors = []
                for sources_file in sources_files:
                    try:
                        with open(sources_file) as f:
                            data = yaml.safe_load(f)

                        # Basic validation
                        if not data:
                            dbt_errors.append(
                                f"{sources_file.relative_to(project_root)}: Empty file"
                            )
                        elif not isinstance(data, dict):
                            dbt_errors.append(
                                f"{sources_file.relative_to(project_root)}: Invalid structure (expected dict)"
                            )
                        elif "sources" not in data:
                            dbt_errors.append(
                                f"{sources_file.relative_to(project_root)}: Missing 'sources' key"
                            )
                        else:
                            # File is valid
                            console.print(
                                f"[green]✓[/green] {sources_file.relative_to(project_root)}"
                            )

                    except yaml.YAMLError as e:
                        all_valid = False
                        line_num = getattr(e, "problem_mark", None)
                        if line_num:
                            dbt_errors.append(
                                f"{sources_file.relative_to(project_root)}: "
                                f"YAML error at line {line_num.line + 1}, column {line_num.column + 1}"
                            )
                        else:
                            dbt_errors.append(
                                f"{sources_file.relative_to(project_root)}: Invalid YAML"
                            )
                    except Exception as e:
                        all_valid = False
                        dbt_errors.append(f"{sources_file.relative_to(project_root)}: {str(e)}")
                        from dango.exceptions import is_debug_mode

                        if is_debug_mode():
                            import traceback

                            console.print(traceback.format_exc())

                if dbt_errors:
                    all_valid = False
                    console.print()
                    console.print("[red]✗[/red] dbt sources.yml errors:")
                    for error in dbt_errors:
                        console.print(f"    • {error}")

        console.print()

        # Summary
        if all_valid:
            console.print("[green]✅ All configuration files are valid[/green]")
        else:
            console.print("[red]❌ Configuration has errors[/red]")
            console.print()
            console.print("[dim]Fix errors and run 'dango config validate' again[/dim]")
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@config.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """
    Show current configuration.
    """
    from rich.syntax import Syntax

    from dango.config import ConfigLoader

    from ..utils import require_project_context

    console.print("🍡 [bold]Current Configuration[/bold]")
    console.print()

    try:
        project_root = require_project_context(ctx)
        loader = ConfigLoader(project_root)

        # Show project.yml
        if loader.project_file.exists():
            with open(loader.project_file) as f:
                project_yaml = f.read()

            console.print("[bold cyan]project.yml:[/bold cyan]")
            syntax = Syntax(project_yaml, "yaml", theme="monokai", line_numbers=True)
            console.print(syntax)
            console.print()

        # Show sources.yml
        if loader.sources_file.exists():
            with open(loader.sources_file) as f:
                sources_yaml = f.read()

            console.print("[bold cyan]sources.yml:[/bold cyan]")
            syntax = Syntax(sources_yaml, "yaml", theme="monokai", line_numbers=True)
            console.print(syntax)
        else:
            console.print("[dim]sources.yml not found (no sources configured yet)[/dim]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


# ---------------------------------------------------------------------------
# DigitalOcean token management (BUG-127)
# ---------------------------------------------------------------------------


@config.group("do-token")
def do_token() -> None:
    """Manage stored DigitalOcean API token."""


@do_token.command("clear")
def do_token_clear() -> None:
    """Remove the stored DigitalOcean API token."""
    from dango.config.cloud_credentials import clear_do_token

    if clear_do_token():
        console.print("[green]DigitalOcean API token removed.[/green]")
    else:
        console.print("[dim]No stored token found.[/dim]")
