"""dango/cli/commands/remote_report.py

Output helpers for ``remote upgrade|resize|migrate``.

Text that comes from the server (warnings, step lines, error messages, paths) is printed
literally: Rich markup in it is escaped, numbers and paths are not highlighted, and long lines
are not wrapped, so that a command in a message pastes as one line and a crafted file name
cannot turn a finished operation into a markup error.
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape

from dango.cli import console


def literal(text: object) -> str:
    """*text* as Rich markup that prints it unchanged."""
    return escape(str(text))


def line(text: object, prefix: str = "  ") -> None:
    """Print server text after *prefix* (plain)."""
    console.print(f"{prefix}{literal(text)}", highlight=False, soft_wrap=True)


def warning(text: object) -> None:
    """Print a warning line."""
    console.print(f"  [yellow]Warning:[/yellow] {literal(text)}", highlight=False, soft_wrap=True)


def failure(what: str, exc: object) -> None:
    """Print ``Error: <what>: <exc>``."""
    console.print(
        f"[red]Error:[/red] {escape(what)}: {literal(exc)}", highlight=False, soft_wrap=True
    )


def upgrade_result(result: Any, done: str) -> None:
    """Print the summary of a finished ``remote upgrade``."""
    console.print()
    style = "green" if result.health_check_passed else "yellow"
    suffix = "." if result.health_check_passed else " with warnings."
    console.print(f"[{style}]{done}{suffix}[/{style}]")
    console.print(
        f"  Version: [cyan]{literal(result.old_version)}[/cyan] → "
        f"[cyan]{literal(result.new_version)}[/cyan]"
    )
    console.print(f"  Duration: {result.duration_seconds}s")
    if result.backup_path:
        line(result.backup_path, "  Backup: ")
    if result.migrations_run:
        console.print("  Migrations: applied")
    if result.docker_rebuilt:
        console.print("  Docker: rebuilt")
    steps = (result.caddyfile_ws_migration, result.runtime_ownership, result.venv_rebuild)
    for text in filter(None, (*steps, result.runner_isolation)):
        line(text)
    for text in result.warnings:
        warning(text)
    if not result.health_check_passed:
        console.print(
            "\n[yellow]Health check failed.[/yellow] "
            "Run [bold]dango remote rollback[/bold] to restore the previous version."
        )
