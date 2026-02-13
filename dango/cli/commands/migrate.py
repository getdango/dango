"""dango/cli/commands/migrate.py

CLI commands for database migration management (status, run).
"""

import click

from dango.cli import console


@click.group()
@click.pass_context
def migrate(ctx: click.Context) -> None:
    """
    Manage database migrations.

    Commands:
      dango migrate status   Show migration status for all databases
      dango migrate run      Apply pending migrations
    """
    pass


@migrate.command("status")
@click.pass_context
def migrate_status(ctx: click.Context) -> None:
    """Show migration status for all databases."""
    from rich.table import Table

    from dango.migrations import get_all_status

    from ..utils import require_project_context

    project_root = require_project_context(ctx)
    statuses = get_all_status(project_root)

    if not statuses:
        console.print("[dim]No migration databases found.[/dim]")
        return

    for ms in statuses:
        table = Table(title=f"Database: {ms.db_name}", show_header=True)
        table.add_column("Version", style="cyan", justify="right")
        table.add_column("Description")
        table.add_column("Status", justify="center")
        table.add_column("Applied At")

        for applied in ms.applied:
            table.add_row(
                str(applied.version),
                applied.description,
                "[green]applied[/green]",
                applied.applied_at,
            )
        for pending in ms.pending:
            table.add_row(
                str(pending.version),
                pending.description,
                "[yellow]pending[/yellow]",
                "",
            )

        console.print(table)
        console.print(
            f"  Current version: [cyan]{ms.current_version}[/cyan]  |  "
            f"Applied: [green]{len(ms.applied)}[/green]  |  "
            f"Pending: [yellow]{len(ms.pending)}[/yellow]"
        )
        console.print()


@migrate.command("run")
@click.option("--db", "db_name", default=None, help="Apply to a specific database only.")
@click.pass_context
def migrate_run(ctx: click.Context, db_name: str | None) -> None:
    """Apply pending migrations."""
    from dango.exceptions import MigrationError
    from dango.migrations import MigrationRunner, apply_all_pending

    from ..utils import require_project_context

    project_root = require_project_context(ctx)

    try:
        if db_name:
            from dango.migrations import _get_migrations_base_dir

            migrations_dir = _get_migrations_base_dir() / db_name
            if not migrations_dir.is_dir():
                console.print(f"[red]Error:[/red] No migration directory for '{db_name}'")
                raise click.Abort()

            db_path = project_root / ".dango" / f"{db_name}.db"
            runner = MigrationRunner(
                db_path=db_path, db_name=db_name, migrations_dir=migrations_dir
            )
            applied = runner.apply_pending()
            results = {db_name: applied}
        else:
            results = apply_all_pending(project_root)

        total = sum(len(v) for v in results.values())
        if total == 0:
            console.print("[green]All databases are up to date.[/green]")
        else:
            for name, applied in results.items():
                if applied:
                    console.print(f"  [green]✓[/green] {name}: applied {len(applied)} migration(s)")
                    for m in applied:
                        console.print(f"    {m.version}: {m.description}")
    except MigrationError as exc:
        console.print(f"[red]Migration error:[/red] {exc}")
        raise click.Abort() from exc
