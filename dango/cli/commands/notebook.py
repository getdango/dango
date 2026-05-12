"""dango/cli/commands/notebook.py

Notebook management CLI commands.
"""

from __future__ import annotations

import click

from dango.cli import console


@click.group(invoke_without_command=True)
@click.pass_context
def notebook(ctx: click.Context) -> None:
    """Manage Marimo notebooks.

    Without a subcommand, lists all notebooks in the project.
    """
    if ctx.invoked_subcommand is not None:
        return

    from rich.table import Table

    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    notebooks_dir = project_root / "notebooks"

    if not notebooks_dir.exists() or not list(notebooks_dir.glob("*.py")):
        console.print(
            "[dim]No notebooks found.[/dim] Run [bold]dango notebook new[/bold] to create one."
        )
        return

    table = Table(title="Notebooks")
    table.add_column("Name", style="bold")
    table.add_column("Author")
    table.add_column("Size")
    table.add_column("Last Modified")

    from datetime import datetime

    # Query notebook_metadata for author info
    authors: dict[str, str] = {}
    try:
        from dango.utils.dango_db import connect

        with connect(project_root) as conn:
            rows = conn.execute("SELECT name, created_by FROM notebook_metadata").fetchall()
            for row in rows:
                authors[row["name"]] = row["created_by"]
    except Exception:
        pass  # DB not initialized yet — show "--" for all

    for f in sorted(notebooks_dir.glob("*.py")):
        stat = f.stat()
        size_kb = stat.st_size / 1024
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        author = authors.get(f.stem, "--")
        table.add_row(f.stem, author, f"{size_kb:.1f} KB", mtime)

    console.print(table)


@notebook.command("new")
@click.option(
    "--template",
    "-t",
    type=click.Choice(["explore", "quality", "blank"]),
    default="explore",
    help="Starter template to use.",
)
@click.option("--name", "-n", required=True, help="Notebook name (no extension).")
@click.pass_context
def notebook_new(ctx: click.Context, template: str, name: str) -> None:
    """Create a new notebook from a starter template."""
    import shutil
    import uuid
    from datetime import datetime
    from pathlib import Path

    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    notebooks_dir = project_root / "notebooks"
    notebooks_dir.mkdir(parents=True, exist_ok=True)

    dest = notebooks_dir / f"{name}.py"
    if dest.exists():
        console.print(f"[red]Error:[/red] Notebook '{name}' already exists.")
        raise SystemExit(1)

    # Copy template
    templates_dir = Path(__file__).parent.parent.parent / "notebooks" / "templates"
    template_file = templates_dir / f"{template}.py"
    if not template_file.exists():
        console.print(f"[red]Error:[/red] Template '{template}' not found.")
        raise SystemExit(1)

    shutil.copy2(str(template_file), str(dest))

    # Register in metadata
    from dango.utils.dango_db import connect

    notebook_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    with connect(project_root) as conn:
        conn.execute(
            "INSERT INTO notebook_metadata (id, name, description, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (notebook_id, name, f"Created from {template} template", "cli", now, now),
        )
        conn.commit()

    # Log audit event
    from dango.auth.audit import AuditEvent, log_auth_event

    log_auth_event(
        AuditEvent.NOTEBOOK_CREATED,
        details={"notebook_name": name, "template": template},
        log_dir=project_root / ".dango" / "logs",
    )

    console.print(
        f"[green]✓[/green] Created notebook [bold]{name}[/bold] from '{template}' template"
    )
    console.print(f"  [dim]{dest}[/dim]")


@notebook.command("open")
@click.argument("name")
@click.pass_context
def notebook_open(ctx: click.Context, name: str) -> None:
    """Open a notebook in Marimo (starts server if needed).

    Acquires a lock, creates a DuckDB snapshot, and starts Marimo.
    Press Ctrl+C to release the lock and exit.
    """
    import threading
    import webbrowser

    from dango.cli.utils import require_project_context
    from dango.notebooks.locking import acquire_lock, get_lock_info, refresh_lock, release_lock
    from dango.notebooks.manager import get_marimo_status, start_marimo
    from dango.notebooks.snapshot import create_snapshot

    project_root = require_project_context(ctx)
    notebooks_dir = project_root / "notebooks"

    nb_path = notebooks_dir / f"{name}.py"
    if not nb_path.exists():
        console.print(f"[red]Error:[/red] Notebook '{name}' not found at {nb_path}")
        raise SystemExit(1)

    # Acquire lock
    acquired = acquire_lock(project_root, name, "cli")
    if not acquired:
        lock = get_lock_info(project_root, name)
        holder = lock["locked_by"] if lock else "another user"
        console.print(f"[red]Error:[/red] Notebook '{name}' is locked by {holder}.")
        raise SystemExit(1)

    # Create snapshot
    snapshot_path = None
    try:
        snapshot_path = create_snapshot(project_root, "cli")
        console.print(f"[green]✓[/green] Created DuckDB snapshot at {snapshot_path.name}")
    except FileNotFoundError:
        pass  # no warehouse yet — notebooks will use default path

    # Start heartbeat thread
    stop_event = threading.Event()

    def _heartbeat() -> None:
        while not stop_event.wait(timeout=300):
            refresh_lock(project_root, name, "cli")

    heartbeat = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat.start()

    try:
        status = get_marimo_status(project_root)
        if not status["running"]:
            console.print("[cyan]Starting Marimo server...[/cyan]")
            pid = start_marimo(project_root, snapshot_path=snapshot_path)
            if pid:
                console.print(f"[green]✓[/green] Marimo started (PID {pid})")
            status = get_marimo_status(project_root)

        port = status.get("port") or 7805
        url = f"http://localhost:{port}/?file={name}.py"
        console.print(f"\n  [bold]Open in browser:[/bold] {url}")

        webbrowser.open(url)

        console.print("\n[dim]Press Ctrl+C to release lock and exit.[/dim]")
        stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        heartbeat.join(timeout=2)
        release_lock(project_root, name, "cli")
        console.print("[green]✓[/green] Lock released.")
