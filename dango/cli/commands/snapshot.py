"""dango/cli/commands/snapshot.py

Snapshot command group: dbt snapshots (SCD Type 2) and DuckDB snapshots.
"""

from __future__ import annotations

import click

from dango.cli import console


@click.group(invoke_without_command=True)
@click.pass_context
def snapshot(ctx: click.Context) -> None:
    """Manage dbt snapshots (SCD Type 2 change tracking) and DuckDB snapshots.

    Without a subcommand, shows this help message.

    \b
    Subcommands:
      add   — Interactive wizard to create a dbt snapshot
      list  — Show configured dbt snapshots
      run   — Execute dbt snapshot
      db    — Create a DuckDB read-only snapshot for notebook use
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@snapshot.command("add")
@click.pass_context
def snapshot_add(ctx: click.Context) -> None:
    """Interactive wizard to create a dbt snapshot (SCD Type 2).

    Walks through selecting a source table, detecting the primary key,
    choosing a snapshot strategy, and generating the snapshot SQL file.
    """
    import re
    from datetime import datetime
    from pathlib import Path

    import duckdb
    import inquirer
    from inquirer import themes

    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    db_path = project_root / "warehouse.duckdb"

    if not db_path.exists():
        console.print(
            "[red]Error:[/red] No warehouse.duckdb found. Run [bold]dango sync[/bold] first."
        )
        raise SystemExit(1)

    # 1. Discover available sources/tables from DuckDB
    conn = None
    try:
        conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
        rows = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema LIKE 'raw_%' AND table_name NOT LIKE '_dlt_%' "
            "ORDER BY table_schema, table_name"
        ).fetchall()
    finally:
        if conn is not None:
            conn.close()

    if not rows:
        console.print(
            "[yellow]No raw tables found.[/yellow] Run [bold]dango sync[/bold] to load data first."
        )
        return

    # Group tables by source
    sources: dict[str, list[str]] = {}
    for schema, table in rows:
        # schema = raw_{source_name}
        source_name = schema.removeprefix("raw_")
        sources.setdefault(source_name, []).append(table)

    # 2. Select source
    source_choices = sorted(sources.keys())
    answers = inquirer.prompt(
        [
            inquirer.List(
                "source",
                message="Select source",
                choices=source_choices + ["← Cancel"],
                carousel=True,
            )
        ],
        theme=themes.GreenPassion(),
    )
    if not answers or answers["source"] == "← Cancel":
        return

    selected_source = answers["source"]

    # 3. Select table
    table_choices = sorted(sources[selected_source])
    answers = inquirer.prompt(
        [
            inquirer.List(
                "table", message="Select table", choices=table_choices + ["← Cancel"], carousel=True
            )
        ],
        theme=themes.GreenPassion(),
    )
    if not answers or answers["table"] == "← Cancel":
        return

    selected_table = answers["table"]

    # 4. Auto-detect primary key + timestamp columns
    conn = None
    try:
        conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position",
            [f"raw_{selected_source}", selected_table],
        ).fetchall()
    finally:
        if conn is not None:
            conn.close()

    column_names = [c[0] for c in columns]

    # Heuristic: exact match on id, uuid, key (not _id suffix — those are FKs)
    pk_candidates = [c for c in column_names if c in ("id", "uuid", "key")]
    suggested_pk = pk_candidates[0] if pk_candidates else None

    if suggested_pk:
        answers = inquirer.prompt(
            [
                inquirer.Text(
                    "unique_key",
                    message=f"Unique key column (detected: {suggested_pk})",
                    default=suggested_pk,
                )
            ],
            theme=themes.GreenPassion(),
        )
    else:
        console.print(f"[dim]Available columns: {', '.join(column_names)}[/dim]")
        answers = inquirer.prompt(
            [inquirer.Text("unique_key", message="Unique key column (required for snapshot)")],
            theme=themes.GreenPassion(),
        )

    if not answers or not answers["unique_key"].strip():
        console.print("[red]Error:[/red] Unique key is required for snapshots.")
        raise SystemExit(1)

    unique_key = answers["unique_key"].strip()

    # 5. Select strategy — detect timestamp columns
    timestamp_cols = [
        c for c in column_names if c in ("updated_at", "modified_at", "last_modified")
    ]
    if timestamp_cols:
        default_strategy = "timestamp"
        console.print(f"[dim]Detected timestamp column: {timestamp_cols[0]}[/dim]")
    else:
        default_strategy = "check"

    strategy_choices = [
        f"timestamp — track changes via a timestamp column{' (recommended)' if default_strategy == 'timestamp' else ''}",
        f"check — track changes by comparing all columns{' (recommended)' if default_strategy == 'check' else ''}",
    ]
    answers = inquirer.prompt(
        [
            inquirer.List(
                "strategy", message="Snapshot strategy", choices=strategy_choices, carousel=True
            )
        ],
        theme=themes.GreenPassion(),
    )
    if not answers:
        return

    strategy = "timestamp" if answers["strategy"].startswith("timestamp") else "check"

    updated_at = ""
    if strategy == "timestamp":
        default_ts = timestamp_cols[0] if timestamp_cols else ""
        answers = inquirer.prompt(
            [inquirer.Text("updated_at", message="Timestamp column name", default=default_ts)],
            theme=themes.GreenPassion(),
        )
        if not answers or not answers["updated_at"].strip():
            console.print("[red]Error:[/red] Timestamp column is required for timestamp strategy.")
            raise SystemExit(1)
        updated_at = answers["updated_at"].strip()

    # 6. Generate snapshot SQL file
    snapshot_name = f"snap_{selected_source}_{selected_table}"
    # Sanitize: only allow alphanumeric and underscores
    snapshot_name = re.sub(r"[^a-zA-Z0-9_]", "_", snapshot_name)

    snapshots_dir = project_root / "dbt" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    output_file = snapshots_dir / f"{snapshot_name}.sql"
    if output_file.exists():
        console.print(f"[yellow]Warning:[/yellow] {output_file.name} already exists.")
        answers = inquirer.prompt(
            [
                inquirer.List(
                    "overwrite", message="Overwrite?", choices=["Yes", "No"], carousel=True
                )
            ],
            theme=themes.GreenPassion(),
        )
        if not answers or answers["overwrite"] == "No":
            console.print("[dim]Cancelled.[/dim]")
            return

    # Render template
    import jinja2

    templates_dir = Path(__file__).parent.parent.parent / "templates" / "dbt"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        keep_trailing_newline=True,
    )
    template = env.get_template("snapshot.sql.j2")
    rendered = template.render(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        snapshot_name=snapshot_name,
        source_name=selected_source,
        table_name=selected_table,
        strategy=strategy,
        unique_key=unique_key,
        updated_at=updated_at,
    )

    output_file.write_text(rendered)

    console.print(f"\n[green]✓[/green] Created snapshot [bold]{snapshot_name}[/bold]")
    console.print(f"  File: {output_file}")
    console.print(f"  Strategy: {strategy}")
    console.print(f"  Unique key: {unique_key}")
    if strategy == "timestamp":
        console.print(f"  Timestamp column: {updated_at}")
    console.print("\n[dim]Run [bold]dango snapshot run[/bold] to execute the snapshot.[/dim]")


@snapshot.command("list")
@click.pass_context
def snapshot_list(ctx: click.Context) -> None:
    """List configured dbt snapshots."""
    import re

    from rich.table import Table

    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    snapshots_dir = project_root / "dbt" / "snapshots"

    if not snapshots_dir.exists() or not list(snapshots_dir.glob("*.sql")):
        console.print("[dim]No dbt snapshots configured.[/dim]")
        console.print("Run [bold]dango snapshot add[/bold] to create one.")
        return

    table = Table(title="dbt Snapshots")
    table.add_column("Name", style="bold")
    table.add_column("Source")
    table.add_column("Strategy")
    table.add_column("Unique Key")
    table.add_column("File")

    for sql_file in sorted(snapshots_dir.glob("*.sql")):
        content = sql_file.read_text()

        # Parse snapshot name from {% snapshot <name> %}
        name_match = re.search(r"\{%\s*snapshot\s+(\w+)\s*%\}", content)
        name = name_match.group(1) if name_match else sql_file.stem

        # Parse source from source('x', 'y')
        source_match = re.search(r"source\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", content)
        source_display = f"{source_match.group(1)}.{source_match.group(2)}" if source_match else "—"

        # Parse strategy
        strategy_match = re.search(r"strategy\s*=\s*'(\w+)'", content)
        strategy_val = strategy_match.group(1) if strategy_match else "—"

        # Parse unique_key
        uk_match = re.search(r"unique_key\s*=\s*'(\w+)'", content)
        uk_val = uk_match.group(1) if uk_match else "—"

        table.add_row(name, source_display, strategy_val, uk_val, sql_file.name)

    console.print(table)


@snapshot.command("run")
@click.option("--select", "-s", default=None, help="Run specific snapshot(s) by name.")
@click.pass_context
def snapshot_run(ctx: click.Context, select: str | None) -> None:
    """Execute dbt snapshot to capture SCD Type 2 change history.

    This acquires a DuckDB write lock and runs `dbt snapshot`.

    Examples:
      dango snapshot run                     Run all snapshots
      dango snapshot run -s snap_shopify_orders   Run specific snapshot
    """
    from dango.cli.utils import require_project_context
    from dango.transformation import run_dbt_snapshots
    from dango.utils import DbtLock, DbtLockError

    project_root = require_project_context(ctx)
    dbt_dir = project_root / "dbt"

    if not dbt_dir.exists():
        console.print("[red]Error:[/red] dbt directory not found.")
        console.print(f"[dim]Expected: {dbt_dir}[/dim]")
        raise SystemExit(1)

    snapshots_dir = dbt_dir / "snapshots"
    if not snapshots_dir.exists() or not list(snapshots_dir.glob("*.sql")):
        console.print("[dim]No dbt snapshots configured.[/dim]")
        console.print("Run [bold]dango snapshot add[/bold] to create one.")
        return

    lock = None
    try:
        lock = DbtLock(
            project_root=project_root,
            source="cli",
            operation=f"dbt snapshot{f' --select {select}' if select else ''}",
        )
        lock.acquire()

        console.print("[dim]Running dbt snapshot...[/dim]\n")
        success, output = run_dbt_snapshots(project_root, select=select)

        if success:
            console.print("[green]✓[/green] dbt snapshot completed successfully.")
        else:
            console.print(f"[red]Error:[/red] {output}")
            raise SystemExit(1)

    except DbtLockError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled[/yellow]")
        raise SystemExit(1) from None
    finally:
        if lock is not None and lock._acquired:
            try:
                lock.release()
            except Exception:
                pass


@snapshot.command("db")
@click.option("--user", "-u", default="default", help="Username for the snapshot.")
@click.pass_context
def snapshot_db(ctx: click.Context, user: str) -> None:
    """Create a DuckDB read-only snapshot for notebook use."""
    from dango.cli.utils import require_project_context
    from dango.notebooks.snapshot import create_snapshot

    project_root = require_project_context(ctx)

    try:
        snap_path = create_snapshot(project_root, username=user)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e

    size_mb = snap_path.stat().st_size / (1024 * 1024)
    console.print(f"[green]✓[/green] Snapshot created: [bold]{snap_path.name}[/bold]")
    console.print(f"  Path: {snap_path}")
    console.print(f"  Size: {size_mb:.1f} MB")
