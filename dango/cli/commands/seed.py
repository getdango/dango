"""dango/cli/commands/seed.py

dbt seed management commands (add, list).

Seeds are the portable way to reference external CSV data from models: drop a
CSV into ``dbt/seeds/`` and reference it with ``{{ ref('seed_name') }}``.
"""

import re
import shutil
from pathlib import Path

import click

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
@click.option(
    "--model",
    "model_name",
    default=None,
    help="Scaffold a staging model stub referencing this seed",
)
@click.pass_context
def seed_add(ctx: click.Context, path: str, model_name: str | None) -> None:
    """
    Copy a CSV file into dbt/seeds/ and report its ref() name.

    Examples:
      dango seed add path/to/data.csv
      dango seed add path/to/data.csv --model int_test
    """
    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)
        src = Path(path)
        seed_name = src.stem

        if not _VALID_DBT_IDENTIFIER_RE.match(seed_name):
            console.print(
                f"[red]Error:[/red] '{src.name}' is not a valid dbt seed name. "
                "Rename the file to use only letters, digits, and underscores "
                "(no spaces or dashes), then try again."
            )
            raise click.Abort()

        if model_name and not _VALID_DBT_IDENTIFIER_RE.match(model_name):
            console.print(
                f"[red]Error:[/red] '{model_name}' is not a valid dbt model name. "
                "Use only letters, digits, and underscores (no spaces or dashes)."
            )
            raise click.Abort()

        seeds_dir = project_root / "dbt" / "seeds"
        seeds_dir.mkdir(parents=True, exist_ok=True)
        dest = seeds_dir / src.name

        if dest.exists():
            console.print(
                f"[yellow]⚠[/yellow] {dest.name} already exists in dbt/seeds/ (overwriting)"
            )
        shutil.copy2(src, dest)

        console.print(f"[green]✓[/green] Copied [bold]{src.name}[/bold] → dbt/seeds/")
        console.print(f"Reference in a model: {{{{ ref('{seed_name}') }}}}")

        if model_name:
            staging_dir = project_root / "dbt" / "models" / "staging"
            model_dest = staging_dir / f"{model_name}.sql"
            if model_dest.exists():
                console.print(
                    f"[yellow]⚠[/yellow] Model 'dbt/models/staging/{model_name}.sql' already exists (skipping)"
                )
            else:
                staging_dir.mkdir(parents=True, exist_ok=True)
                model_sql = (
                    "{{ config(materialized='table', schema='staging') }}\n\n"
                    f"SELECT *\nFROM {{{{ ref('{seed_name}') }}}}\n"
                )
                model_dest.write_text(model_sql, encoding="utf-8")
                console.print(
                    f"[green]✓[/green] Scaffolded model: dbt/models/staging/{model_name}.sql"
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
            console.print(f"  {f.name}  →  {{{{ ref('{f.stem}') }}}}")

    except click.Abort:
        raise
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort() from e
