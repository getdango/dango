"""dango/cli/commands/seed.py

dbt seed management commands (add, list).

Seeds are the portable way to reference external CSV data from models: drop a
CSV into ``dbt/seeds/`` and reference it with ``{{ ref('seed_name') }}``.
"""

import re
import shutil
from pathlib import Path

import click
import yaml

from dango.cli import console

# dbt identifier rules: start with a letter/underscore, then letters/digits/underscores.
_VALID_DBT_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@click.group()
@click.pass_context
def seed(ctx: click.Context) -> None:
    """
    Manage dbt seed CSV files.

    Seeds are the portable way to reference external CSV data from models:
    add a CSV to dbt/seeds/ and reference it with {{ ref('seed_name') }}.

    Commands:
      dango seed add     Copy a CSV into dbt/seeds/
      dango seed list    List seeds and their ref() names
    """
    pass


@seed.command("add")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def seed_add(ctx: click.Context, path: str) -> None:
    """
    Copy a CSV file into dbt/seeds/ and report its ref() name.

    Seeds are reference data (lookup tables, mappings). Reference them
    directly in mart or intermediate models via {{ ref('seed_name') }}.
    Do NOT create staging wrappers for seeds — they are not source data.

    Example:
      dango seed add path/to/data.csv
    """
    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)
        src = Path(path)

        if src.suffix.lower() != ".csv":
            console.print(
                f"[red]Error:[/red] '{src.name}' is not a CSV file. dbt seeds must be CSV files."
            )
            raise click.Abort()

        seed_name = src.stem

        if not _VALID_DBT_IDENTIFIER_RE.match(seed_name):
            console.print(
                f"[red]Error:[/red] '{src.name}' is not a valid dbt seed name. "
                "Rename the file to use only letters, digits, and underscores "
                "(no spaces or dashes), then try again."
            )
            raise click.Abort()

        seeds_dir = project_root / "dbt" / "seeds"
        seeds_dir.mkdir(parents=True, exist_ok=True)
        dest = seeds_dir / src.name

        dbt_project_path = project_root / "dbt" / "dbt_project.yml"
        schema_configured = False

        if dbt_project_path.exists():
            try:
                with open(dbt_project_path, encoding="utf-8") as f:
                    dbt_data = yaml.safe_load(f) or {}

                project_name = dbt_data.get("name", "")

                if not project_name:
                    console.print(
                        "[yellow]⚠[/yellow] dbt_project.yml has no 'name' key. "
                        "Seed will be added, but schema config skipped."
                    )
                else:
                    seeds_cfg = dbt_data.setdefault("seeds", {})
                    if not isinstance(seeds_cfg, dict):
                        console.print(
                            "[yellow]⚠[/yellow] dbt_project.yml 'seeds' config is malformed (not a dict). "
                            "Seed will be added, but schema config skipped. "
                            f"Please fix 'seeds' in {dbt_project_path}."
                        )
                    else:
                        proj_seeds = seeds_cfg.setdefault(project_name, {})
                        if proj_seeds.get("+schema") != "seeds":
                            proj_seeds["+schema"] = "seeds"
                            with open(dbt_project_path, "w", encoding="utf-8") as f:
                                yaml.safe_dump(
                                    dbt_data, f, default_flow_style=False, allow_unicode=True
                                )
                            schema_configured = True
                        else:
                            schema_configured = True
            except yaml.YAMLError as e:
                console.print(
                    f"[yellow]⚠[/yellow] Failed to parse dbt_project.yml: {e}. "
                    "Seed will be added, but schema config skipped."
                )
        else:
            console.print(
                "[yellow]⚠[/yellow] dbt_project.yml not found. "
                "Seed will be added, but schema config skipped. "
                "Run 'dango init' to create project config."
            )

        if dest.exists():
            console.print(
                f"[yellow]⚠[/yellow] {dest.name} already exists in dbt/seeds/ (overwriting)"
            )
        shutil.copy2(src, dest)

        console.print(f"[green]✓[/green] Copied [bold]{src.name}[/bold] → dbt/seeds/")
        if schema_configured:
            console.print("[green]✓[/green] Schema configured: seeds → seeds schema")
        console.print(f"Reference in a model: {{{{ ref('{seed_name}') }}}}", highlight=False)
        console.print(
            "[dim]Tip: Reference seeds directly in mart/intermediate models. "
            "Do NOT create staging wrappers for seeds.[/dim]"
        )

    except click.Abort:
        raise
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@seed.command("list")
@click.pass_context
def seed_list(ctx: click.Context) -> None:
    """
    List seed CSV files in dbt/seeds/ with their ref() names.
    """
    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)
        seeds_dir = project_root / "dbt" / "seeds"

        if not seeds_dir.exists():
            console.print(
                "[dim]No dbt/seeds/ directory yet. Run 'dango seed add <file.csv>'.[/dim]"
            )
            return

        csv_files = sorted(seeds_dir.glob("*.csv"))
        if not csv_files:
            console.print(
                "[dim]No seed files in dbt/seeds/. Run 'dango seed add <file.csv>'.[/dim]"
            )
            return

        console.print("[bold]dbt seeds:[/bold]")
        for f in csv_files:
            console.print(f"  {f.name}  →  {{{{ ref('{f.stem}') }}}}", highlight=False)

    except click.Abort:
        raise
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort() from e
