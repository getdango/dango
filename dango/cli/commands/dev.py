"""dango/cli/commands/dev.py

Branch-based dbt development — run dbt against a copy of the production
database so that the real warehouse is never modified.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import click
from rich.table import Table

from dango.cli import console

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _get_dev_dir(project_root: Path) -> Path:
    """Return the dev workspace directory (``.dango/dev/``)."""
    return project_root / ".dango" / "dev"


def _read_dbt_profile_name(project_root: Path) -> str:
    """Read the profile name from ``dbt/dbt_project.yml``.

    The profile name is the value of the top-level ``profile:`` key.

    Raises:
        click.ClickException: If the file is missing or unparseable.
    """
    dbt_project_path = project_root / "dbt" / "dbt_project.yml"
    if not dbt_project_path.exists():
        raise click.ClickException(f"dbt_project.yml not found at {dbt_project_path}")

    try:
        # Use PyYAML (available via dbt dependency) for reliable parsing
        import yaml

        with open(dbt_project_path) as f:
            data = yaml.safe_load(f)
        profile_name: str = data["profile"]
        # Strip surrounding quotes that may be in the YAML value
        return profile_name.strip("'\"")
    except Exception as exc:
        raise click.ClickException(
            f"Could not read profile name from {dbt_project_path}: {exc}"
        ) from exc


def _create_dev_database(project_root: Path, dev_dir: Path) -> Path:
    """Copy the production warehouse to the dev directory.

    Returns:
        Path to the dev database file.

    Raises:
        click.ClickException: If the production database does not exist.
    """
    prod_db = project_root / "data" / "warehouse.duckdb"
    if not prod_db.exists():
        raise click.ClickException(
            f"Production database not found at {prod_db}.\nRun 'dango sync' first to load data."
        )

    dev_dir.mkdir(parents=True, exist_ok=True)
    dev_db = dev_dir / "warehouse_dev.duckdb"

    console.print(
        f"[dim]Copying production database ({_format_size(prod_db.stat().st_size)})...[/dim]"
    )
    shutil.copy2(str(prod_db), str(dev_db))

    # Also copy any WAL file if present (DuckDB may have one)
    wal_file = prod_db.with_suffix(".duckdb.wal")
    if wal_file.exists():
        shutil.copy2(str(wal_file), str(dev_db.with_suffix(".duckdb.wal")))

    return dev_db


def _create_dev_profile(dev_dir: Path, profile_name: str, dev_db_path: Path) -> Path:
    """Generate a ``profiles.yml`` in *dev_dir* pointing at the dev database.

    Uses an **absolute path** to the dev DB to avoid dbt-duckdb path
    resolution ambiguity.

    Returns:
        Path to the generated profiles.yml.
    """
    profiles_content = (
        f"{profile_name}:\n"
        f"  target: dev\n"
        f"  outputs:\n"
        f"    dev:\n"
        f"      type: duckdb\n"
        f"      path: {dev_db_path}\n"
        f"      schema: main\n"
        f"      threads: 4\n"
        f"      extensions:\n"
        f"        - httpfs\n"
        f"        - parquet\n"
        f"      settings:\n"
        f"        memory_limit: 4GB\n"
        f"        threads: 4\n"
    )
    profiles_path = dev_dir / "profiles.yml"
    profiles_path.write_text(profiles_content)
    return profiles_path


def _run_dev_dbt(project_root: Path, dev_dir: Path, select: str | None) -> int:
    """Run ``dbt run`` against the dev profile.

    Returns:
        The subprocess return code.
    """
    from dango.transformation import _get_dbt_executable

    dbt_cmd = _get_dbt_executable()
    dbt_dir = project_root / "dbt"

    cmd = [dbt_cmd, "run", "--project-dir", str(dbt_dir), "--profiles-dir", str(dev_dir)]
    if select:
        cmd.extend(["--select", select])

    console.print(f"[dim]Running: {' '.join(cmd)}[/dim]\n")

    result = subprocess.run(cmd, cwd=str(dbt_dir), timeout=300)
    return result.returncode


def _parse_run_results(project_root: Path) -> list[dict[str, Any]]:
    """Parse ``dbt/target/run_results.json`` and return a summary list.

    Each entry has keys: ``name``, ``status``, ``execution_time``.
    Returns an empty list if the file is missing or unparseable.
    """
    run_results_path = project_root / "dbt" / "target" / "run_results.json"
    if not run_results_path.exists():
        return []

    try:
        with open(run_results_path) as f:
            data = json.load(f)
    except Exception:
        return []

    results: list[dict[str, Any]] = []
    for entry in data.get("results", []):
        unique_id = entry.get("unique_id", "")
        if not unique_id.startswith("model."):
            continue
        # e.g. "model.my_project.stg_orders" → "stg_orders"
        name = unique_id.rsplit(".", 1)[-1] if "." in unique_id else unique_id
        results.append(
            {
                "name": name,
                "status": entry.get("status", "unknown"),
                "execution_time": round(entry.get("execution_time", 0), 2),
            }
        )
    return results


def _print_results_summary(results: list[dict[str, Any]]) -> None:
    """Print a Rich table summarising dbt run results."""
    if not results:
        console.print("[yellow]No model results found.[/yellow]")
        return

    table = Table(title="dbt run results", show_header=True, header_style="bold cyan")
    table.add_column("Model", style="white")
    table.add_column("Status", style="bold")
    table.add_column("Time (s)", justify="right", style="dim")

    for r in results:
        status_str = r["status"]
        if status_str == "success":
            status_display = "[green]success[/green]"
        elif status_str == "error":
            status_display = "[red]error[/red]"
        else:
            status_display = f"[yellow]{status_str}[/yellow]"
        table.add_row(r["name"], status_display, str(r["execution_time"]))

    console.print(table)

    passed = sum(1 for r in results if r["status"] == "success")
    failed = sum(1 for r in results if r["status"] == "error")
    skipped = len(results) - passed - failed
    parts = [f"[green]{passed} passed[/green]"]
    if failed:
        parts.append(f"[red]{failed} failed[/red]")
    if skipped:
        parts.append(f"[yellow]{skipped} skipped[/yellow]")
    console.print(f"\n  {', '.join(parts)}")


def _show_row_count_diff(project_root: Path, dev_dir: Path) -> None:
    """Query both prod and dev DBs (read-only) and print row count comparison."""
    import duckdb

    prod_db = project_root / "data" / "warehouse.duckdb"
    dev_db = dev_dir / "warehouse_dev.duckdb"

    if not prod_db.exists() or not dev_db.exists():
        console.print("[yellow]Cannot compare — database file(s) missing.[/yellow]")
        return

    schemas = ("staging", "intermediate", "marts")

    def _get_row_counts(db_path: Path) -> dict[str, int]:
        counts: dict[str, int] = {}
        conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
        try:
            for schema in schemas:
                try:
                    tables = conn.execute(
                        "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
                        [schema],
                    ).fetchall()
                except Exception:
                    continue
                for (table_name,) in tables:
                    try:
                        row = conn.execute(
                            f'SELECT COUNT(*) FROM "{schema}"."{table_name}"'
                        ).fetchone()
                        counts[f"{schema}.{table_name}"] = row[0] if row else 0
                    except Exception:
                        counts[f"{schema}.{table_name}"] = -1
        finally:
            conn.close()
        return counts

    prod_counts = _get_row_counts(prod_db)
    dev_counts = _get_row_counts(dev_db)

    all_tables = sorted(set(prod_counts) | set(dev_counts))
    if not all_tables:
        console.print("[dim]No tables found in staging/intermediate/marts schemas.[/dim]")
        return

    table = Table(title="Row count comparison", show_header=True, header_style="bold cyan")
    table.add_column("Table", style="white")
    table.add_column("Production", justify="right")
    table.add_column("Dev", justify="right")
    table.add_column("Diff", justify="right")

    for tbl in all_tables:
        prod = prod_counts.get(tbl, 0)
        dev_val = dev_counts.get(tbl, 0)
        diff = dev_val - prod
        if diff > 0:
            diff_str = f"[green]+{diff}[/green]"
        elif diff < 0:
            diff_str = f"[red]{diff}[/red]"
        else:
            diff_str = "[dim]0[/dim]"
        table.add_row(tbl, str(prod), str(dev_val), diff_str)

    console.print(table)


def _format_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.option(
    "--select", "-s", default=None, help="dbt model selection (e.g. 'stg_*', 'my_model+')."
)
@click.option("--diff", "show_diff", is_flag=True, help="Show row-count comparison after run.")
@click.pass_context
def dev(ctx: click.Context, select: str | None, show_diff: bool) -> None:
    """Run dbt against a copy of the production database.

    Copies data/warehouse.duckdb to .dango/dev/warehouse_dev.duckdb,
    runs dbt against the copy, and shows results. The production
    database is never modified.

    The dev database persists after the run so you can inspect it.
    Use 'dango dev clean' to remove it.

    Examples:

      dango dev                Run all models against dev copy

      dango dev -s stg_orders  Run specific model(s)

      dango dev --diff         Show row-count diff after run

      dango dev clean          Remove dev artifacts
    """
    if ctx.invoked_subcommand is not None:
        return

    from dango.cli.utils import require_project_context

    try:
        project_root = require_project_context(ctx)
        dbt_dir = project_root / "dbt"

        if not dbt_dir.exists():
            console.print("[red]Error:[/red] dbt directory not found.")
            console.print(f"[dim]Expected: {dbt_dir}[/dim]")
            raise click.Abort()

        # 1. Prepare dev environment
        dev_dir = _get_dev_dir(project_root)
        profile_name = _read_dbt_profile_name(project_root)
        dev_db = _create_dev_database(project_root, dev_dir)
        _create_dev_profile(dev_dir, profile_name, dev_db)

        console.print(f"[green]✓[/green] Dev environment ready at [bold]{dev_dir}[/bold]")
        console.print()

        # 2. Run dbt against dev profile
        returncode = _run_dev_dbt(project_root, dev_dir, select)

        # 3. Parse and display results
        results = _parse_run_results(project_root)
        if results:
            console.print()
            _print_results_summary(results)

        if returncode != 0:
            console.print(f"\n[red]dbt run failed with exit code {returncode}[/red]")
        else:
            console.print("\n[green]✓ Dev run completed successfully.[/green]")

        # 4. Optional row-count diff
        if show_diff:
            console.print()
            _show_row_count_diff(project_root, dev_dir)

        # 5. Helpful footer
        console.print()
        console.print(f"[dim]Dev database: {dev_db}[/dim]")
        console.print("[dim]Run 'dango dev clean' to remove dev artifacts.[/dim]")

    except click.Abort:
        raise
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        raise click.Abort() from None
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@dev.command("clean")
@click.pass_context
def dev_clean(ctx: click.Context) -> None:
    """Remove the dev database and related artifacts.

    Deletes the .dango/dev/ directory created by 'dango dev'.
    """
    from dango.cli.utils import require_project_context

    try:
        project_root = require_project_context(ctx)
        dev_dir = _get_dev_dir(project_root)

        if not dev_dir.exists():
            console.print("[dim]Nothing to clean — no dev artifacts found.[/dim]")
            return

        # Calculate size before removal
        total_size = sum(f.stat().st_size for f in dev_dir.rglob("*") if f.is_file())
        console.print(f"Removing {dev_dir} ({_format_size(total_size)})...")

        shutil.rmtree(dev_dir)
        console.print("[green]✓[/green] Dev artifacts removed.")

    except click.Abort:
        raise
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        raise click.Abort() from None
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e
