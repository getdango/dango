"""dango/cli/commands/source.py

Data source management commands (add, list, remove) and sync.
"""

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import click

from dango.cli import console
from dango.cli.utils import safe_confirm
from dango.config.helpers import (
    check_unreferenced_custom_sources,
    format_unreferenced_sources_warning,
)

if TYPE_CHECKING:
    from pathlib import Path

    from dango.config.models import DataSource

_DURATION_PATTERN = re.compile(r"^(\d+)([dwmDWM])$")
_DURATION_MULTIPLIERS = {"d": 1, "w": 7, "m": 30}


def _parse_duration(value: str) -> int:
    """Parse a duration string like '7d', '2w', '1m' into days.

    Args:
        value: Duration string (e.g. '7d', '30d', '2w', '1m').

    Returns:
        Number of days.

    Raises:
        click.BadParameter: If the format is invalid or value is zero.
    """
    match = _DURATION_PATTERN.match(value)
    if not match:
        raise click.BadParameter(
            f"Invalid duration '{value}'. Use format like '7d', '2w', '1m' "
            "(d=days, w=weeks, m=months/30d)."
        )
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount <= 0:
        raise click.BadParameter("Duration must be a positive number.")
    return amount * _DURATION_MULTIPLIERS[unit]


def _source_supports_date_range(source_type: str) -> bool:
    """Check whether a source type supports start_date/end_date filtering.

    Looks up the source in the registry and checks for a 'start_date'
    parameter in optional_params or required_params.

    Args:
        source_type: Source type key (e.g. 'facebook_ads', 'csv').

    Returns:
        True if the source has a start_date parameter.
    """
    from dango.ingestion.sources.registry import get_source_metadata

    metadata = get_source_metadata(source_type)
    if not metadata:
        return False
    for param_list_key in ("optional_params", "required_params"):
        for param in metadata.get(param_list_key, []):
            if param.get("name") == "start_date":
                return True
    return False


@click.group()
def source() -> None:
    """
    Manage data sources.

    Commands:
      dango source add            Add a new data source
      dango source list           List all sources
      dango source remove         Remove a source
      dango source edit           Open sources.yml in $EDITOR
      dango source inspect-state  Show dlt's incremental cursor state (read-only)
    """
    pass


@source.command("add")
@click.pass_context
def source_add(ctx: click.Context) -> None:
    """
    Add a new data source (interactive wizard).

    Supports 35+ data source types across marketing/analytics, business/CRM,
    e-commerce, files/storage, databases, streaming, development, and
    communication tools. Run this command to see the full current list.
    """
    from dango.cli.source_wizard import add_source

    from ..utils import check_v01x_project

    check_v01x_project()

    project_root = ctx.obj.get("project_root")
    if not project_root:
        console.print("[red]❌ Not in a dango project directory[/red]")
        return

    # Run wizard
    success = add_source(project_root)

    if not success:
        console.print("\n[yellow]Source not added[/yellow]")
        raise click.Abort()


@source.command("edit")
@click.argument("name", required=False)
@click.pass_context
def source_edit(ctx: click.Context, name: str | None) -> None:
    """Open sources.yml in your default editor ($EDITOR).

    Optionally specify a source NAME to focus on that section.

    Examples:
      dango source edit            Edit full sources.yml
      dango source edit chess      Edit sources.yml (hints at chess section)
    """
    from pathlib import Path

    import yaml

    project_root = ctx.obj.get("project_root")
    if not project_root:
        console.print("[red]Not in a dango project directory[/red]")
        return

    sources_file = Path(project_root) / ".dango" / "sources.yml"
    if not sources_file.exists():
        console.print("[red]No sources.yml found[/red]")
        console.print("[dim]Run 'dango source add' first[/dim]")
        return

    # If a name is given, validate it exists
    if name:
        try:
            data = yaml.safe_load(sources_file.read_text()) or {}
            source_names = [s.get("name") for s in data.get("sources", []) if s.get("name")]
            if name not in source_names:
                console.print(f"[red]Error:[/red] Source '{name}' not found")
                if source_names:
                    console.print(f"[dim]Available sources: {', '.join(source_names)}[/dim]")
                return
        except Exception:
            pass  # Fall through to editor

    import os

    # Check if an editor is available — $EDITOR or $VISUAL
    has_editor = bool(os.environ.get("EDITOR") or os.environ.get("VISUAL"))

    if not has_editor:
        console.print(f"[bold]Edit your sources at:[/bold] {sources_file}")
        console.print("[dim]Tip: Set $EDITOR to open in your preferred editor[/dim]")
        return

    original = sources_file.read_text()
    edited = click.edit(original, extension=".yml")

    if edited is None:
        if name:
            console.print("[yellow]No changes made.[/yellow]")
            # Print just the named source section
            try:
                data = yaml.safe_load(original) or {}
                for src in data.get("sources", []):
                    if src.get("name") == name:
                        console.print(f"\n[bold]Source '{name}':[/bold]")
                        console.print(yaml.dump({"sources": [src]}, default_flow_style=False))
                        console.print(f"[dim]File: {sources_file}[/dim]")
                        return
            except Exception:
                pass
        console.print("[yellow]No changes made.[/yellow]")
        console.print(f"[dim]Edit manually: {sources_file}[/dim]")
        return

    if edited == original:
        console.print("[dim]No changes detected.[/dim]")
        return

    # Validate YAML syntax
    try:
        yaml.safe_load(edited)
    except yaml.YAMLError as e:
        console.print(f"[red]Invalid YAML:[/red] {e}")
        console.print("[yellow]Changes NOT saved.[/yellow]")
        return

    sources_file.write_text(edited)
    console.print("[green]sources.yml updated[/green]")

    if name:
        console.print(f"[dim]Hint: Look for 'name: {name}' in the sources list[/dim]")


@source.command("list")
@click.option("--enabled-only", is_flag=True, help="Show only enabled sources")
@click.pass_context
def source_list(ctx: click.Context, enabled_only: bool) -> None:
    """
    List all configured data sources.

    Shows source name, type, status (enabled/disabled), and last sync time.

    Examples:
      dango source list               List all sources
      dango source list --enabled-only  List only enabled sources
    """
    from datetime import datetime

    import duckdb
    from rich.table import Table

    from dango.config import get_config

    from ..utils import require_project_context

    console.print("🍡 [bold]Data Sources[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        config = get_config(project_root)

        # Check for unreferenced custom sources
        unreferenced = check_unreferenced_custom_sources(project_root, config.sources)
        if unreferenced:
            console.print(format_unreferenced_sources_warning(unreferenced))

        # Get sources
        sources = config.sources.sources

        if not sources:
            console.print("[yellow]No sources configured yet[/yellow]")
            console.print("\nRun '[cyan]dango source add[/cyan]' to add a source")
            return

        # Filter if needed
        if enabled_only:
            sources = [s for s in sources if s.enabled]
            if not sources:
                console.print("[yellow]No enabled sources found[/yellow]")
                return

        # Get last sync times from multiple sources:
        # 1. _dlt_loads table in each raw_{source_name} schema (for dlt-based sources)
        # 2. _dango_file_metadata table in main schema (for CSV sources)
        last_sync_times: dict[str, datetime] = {}
        row_counts: dict[str, int] = {}
        duckdb_path = project_root / "data" / "warehouse.duckdb"
        if duckdb_path.exists():
            try:
                conn = duckdb.connect(str(duckdb_path), config={"access_mode": "read_only"})
                try:
                    # Method 1: Check _dlt_loads tables for dlt-based sources
                    # Each source has raw_{source_name}._dlt_loads with inserted_at timestamp
                    for src in sources:
                        raw_schema = f"raw_{src.name}"
                        try:
                            # Check if _dlt_loads table exists for this source
                            result = conn.execute(f"""
                                SELECT MAX(inserted_at) as last_sync
                                FROM "{raw_schema}"._dlt_loads
                                WHERE status = 0
                            """).fetchone()
                            if result and result[0]:
                                # Convert to naive datetime if timezone-aware
                                last_sync_dt = result[0]
                                if hasattr(last_sync_dt, "replace") and last_sync_dt.tzinfo:
                                    last_sync_dt = last_sync_dt.replace(tzinfo=None)
                                last_sync_times[src.name] = last_sync_dt
                        except Exception:
                            # Table doesn't exist for this source, continue
                            pass

                    # Method 2: Check CSV metadata table (may override with more recent time)
                    tables = conn.execute("""
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema = 'main' AND table_name = '_dango_file_metadata'
                    """).fetchall()

                    if tables:
                        result = conn.execute("""
                            SELECT source_name, MAX(loaded_at) as last_sync
                            FROM _dango_file_metadata
                            WHERE status = 'loaded'
                            GROUP BY source_name
                        """).fetchall()

                        for source_name, last_sync in result:
                            # Only update if more recent than dlt load time
                            if (
                                source_name not in last_sync_times
                                or last_sync > last_sync_times[source_name]
                            ):
                                last_sync_times[source_name] = last_sync

                    # Get row counts per source
                    for src in sources:
                        raw_schema = f"raw_{src.name}"
                        try:
                            schema_tables = conn.execute(
                                """
                                SELECT table_name FROM information_schema.tables
                                WHERE table_schema = ?
                                  AND table_name NOT LIKE '\\_dlt\\_%' ESCAPE '\\'
                                  AND table_name NOT LIKE '\\_dango\\_%' ESCAPE '\\'
                                """,
                                [raw_schema],
                            ).fetchall()
                            total = 0
                            for (tbl,) in schema_tables:
                                cnt = conn.execute(
                                    f'SELECT COUNT(*) FROM "{raw_schema}"."{tbl}"'
                                ).fetchone()
                                if cnt:
                                    total += cnt[0]
                            if total > 0:
                                row_counts[src.name] = total
                        except Exception:
                            pass
                finally:
                    conn.close()
            except Exception:
                # If we can't read metadata, just skip - last_sync will show "never"
                pass

        # Build sync mode lookup from registry capabilities
        from dango.ingestion.sources.registry import get_source_capabilities

        def _get_sync_mode(source_type: str) -> str:
            """Determine sync mode badge for a source type."""
            if source_type in ("csv", "local_files"):
                return "Full Refresh"
            caps = get_source_capabilities(source_type)
            if caps and caps.get("incremental"):
                return "Incremental"
            return "Full Refresh"

        # Create table
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Name", style="white", no_wrap=False, min_width=30)
        table.add_column("Type", style="dim")
        table.add_column("Mode", style="dim")
        table.add_column("Status", style="white")
        table.add_column("Last Sync", style="dim")
        table.add_column("Rows", style="dim", justify="right")

        for src in sources:
            # Status indicator
            if src.enabled:
                status = "[green]✓ enabled[/green]"
            else:
                status = "[dim]✗ disabled[/dim]"

            # Last sync time from metadata
            if src.name in last_sync_times:
                last_sync_dt = last_sync_times[src.name]
                # Format: "2 hours ago", "3 days ago", or full date if older
                now = datetime.now()
                diff = now - last_sync_dt

                if diff.days == 0:
                    if diff.seconds < 3600:
                        minutes = diff.seconds // 60
                        last_sync = f"{minutes}m ago" if minutes > 0 else "just now"
                    else:
                        hours = diff.seconds // 3600
                        last_sync = f"{hours}h ago"
                elif diff.days == 1:
                    last_sync = "yesterday"
                elif diff.days < 7:
                    last_sync = f"{diff.days}d ago"
                else:
                    last_sync = last_sync_dt.strftime("%Y-%m-%d")
            else:
                last_sync = "[dim]never[/dim]"

            rows_str = f"{row_counts[src.name]:,}" if src.name in row_counts else "[dim]-[/dim]"
            mode = _get_sync_mode(src.type.value)
            table.add_row(src.name, src.type.value, mode, status, last_sync, rows_str)

        console.print(table)
        console.print()

        # Summary
        enabled_count = sum(1 for s in config.sources.sources if s.enabled)
        total_count = len(config.sources.sources)
        console.print(
            f"[dim]Total: {total_count} sources ({enabled_count} enabled, {total_count - enabled_count} disabled)[/dim]"
        )

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@source.command("remove")
@click.argument("source_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def source_remove(ctx: click.Context, source_name: str, yes: bool) -> None:
    """
    Remove a data source.

    SOURCE_NAME: Name of source to remove

    Examples:
      dango source remove my_csv          Remove source (with confirmation)
      dango source remove my_csv --yes    Remove without confirmation
    """
    from rich.prompt import Confirm

    from dango.config import get_config

    from ..utils import check_git_branch_warning, require_project_context

    console.print(f"🍡 [bold]Removing source: {source_name}[/bold]\n")

    try:
        project_root = require_project_context(ctx)

        # Check git branch (gentle reminder if on main/master)
        check_git_branch_warning(project_root)

        config = get_config(project_root)

        # Check if source exists
        src = config.sources.get_source(source_name)
        if not src:
            console.print(f"[red]Error:[/red] Source '{source_name}' not found")
            console.print("\nAvailable sources:")
            for s in config.sources.sources:
                console.print(f"  • {s.name} ({s.type.value})")
            raise click.Abort()

        # Show source info
        console.print("[bold]Source Details:[/bold]")
        console.print(f"  Name: {src.name}")
        console.print(f"  Type: {src.type.value}")
        console.print(f"  Status: {'enabled' if src.enabled else 'disabled'}")
        console.print()

        # Confirm deletion
        if not yes:
            console.print("[yellow]⚠️  This will remove the source configuration[/yellow]")
            console.print("[dim]Note: This does NOT delete data from DuckDB[/dim]")
            console.print("[dim]      Use 'dango db clean' afterwards to remove data[/dim]\n")

            if not Confirm.ask(f"Remove source '{source_name}'?"):
                console.print("[yellow]Cancelled[/yellow]")
                return

        # Remove from sources.yml
        sources_file = project_root / ".dango" / "sources.yml"
        if not sources_file.exists():
            console.print("[red]Error:[/red] sources.yml not found")
            raise click.Abort()

        # Read YAML
        import yaml

        with open(sources_file) as f:
            data = yaml.safe_load(f) or {}

        # Remove source
        if "sources" in data and isinstance(data["sources"], list):
            data["sources"] = [s for s in data["sources"] if s.get("name") != source_name]

            # Write back
            with open(sources_file, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)

            # Clean up .dlt/config.toml
            config_toml = project_root / ".dlt" / "config.toml"
            if config_toml.exists():
                try:
                    import tomlkit

                    doc = tomlkit.parse(config_toml.read_text())
                    sources_section = doc.get("sources", {})
                    if src.type.value in sources_section:
                        del sources_section[src.type.value]
                        if not sources_section:
                            del doc["sources"]
                        config_toml.write_text(tomlkit.dumps(doc))
                        console.print("[green]✓[/green] Cleaned up .dlt/config.toml")
                except Exception as e:
                    console.print(f"[dim]Could not clean up config.toml: {e}[/dim]")

            # Clean up dbt staging files for this source
            # Naming: stg_{name}__*.sql, sources_{name}.yml, stg_{name}.yml
            staging_dir = project_root / "dbt" / "models" / "staging"
            if staging_dir.exists():
                removed_files = []
                for sql_file in staging_dir.glob(f"stg_{source_name}__*.sql"):
                    sql_file.unlink()
                    removed_files.append(sql_file.name)
                for yml_name in [
                    f"sources_{source_name}.yml",
                    f"stg_{source_name}.yml",
                ]:
                    yml_file = staging_dir / yml_name
                    if yml_file.exists():
                        yml_file.unlink()
                        removed_files.append(yml_name)
                if removed_files:
                    console.print(
                        f"[green]✓[/green] Removed {len(removed_files)} dbt staging file(s)"
                    )

            # Regenerate dbt docs to reflect source removal
            console.print("[dim]Regenerating dbt documentation...[/dim]")
            try:
                from dango.transformation import generate_dbt_docs

                generate_dbt_docs(project_root)
                console.print("[green]✓[/green] dbt documentation regenerated")
            except Exception:
                console.print(
                    "[dim]Could not regenerate dbt documentation — catalog will update on next sync.[/dim]"
                )

            # Offer to clean up related .env variables
            env_file = project_root / ".env"
            if env_file.exists():
                from dango.utils.env_file import parse_env_file, serialize_env_file

                env_content = env_file.read_text()
                env_vars = parse_env_file(env_content)
                source_token = source_name.upper().replace("-", "_")
                matching = {
                    k: v
                    for k, v in env_vars.items()
                    if k.startswith(source_token + "_") or k == source_token
                }

                if matching:
                    console.print("[dim]Found related environment variables in .env:[/dim]")
                    for k in matching:
                        console.print(f"  [cyan]{k}[/cyan]")
                    console.print()
                    if Confirm.ask("Remove these environment variables?", default=False):
                        for k in matching:
                            del env_vars[k]
                        env_file.write_text(serialize_env_file(env_vars))
                        console.print(f"[green]✓[/green] Removed {len(matching)} env variable(s)")
                    else:
                        console.print("[dim]Environment variables left unchanged.[/dim]")
                else:
                    console.print("[dim]No related environment variables found in .env.[/dim]")
            else:
                console.print("[dim]No .env file found.[/dim]")

            console.print()
            console.print(f"[green]✅ Source '{source_name}' removed successfully[/green]")
            console.print()
            console.print("[yellow]⚠️  Important:[/yellow]")
            console.print("  • Source configuration removed from sources.yml")
            console.print("  • [bold]Data still exists[/bold] in DuckDB tables:")
            console.print(f"    - raw.{source_name}")
            console.print(f"    - staging.{source_name}")
            console.print()
            console.print("[dim]To clean up orphaned tables:[/dim]")
            console.print(
                "  [cyan]dango db clean[/cyan]  # Removes tables without source config (including this one)"
            )
            console.print()
            console.print("[dim]Or to check data before cleanup:[/dim]")
            console.print(f'  [cyan]dango db query "SELECT COUNT(*) FROM raw.{source_name}"[/cyan]')
            console.print()

        else:
            console.print("[red]Error:[/red] Invalid sources.yml format")
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


def _resolve_pipeline_and_dataset_name(src: "DataSource") -> tuple[str, str]:
    """
    Resolve a source's dlt pipeline_name and dataset_name the same way dlt_runner.py does.

    - dlt_native sources: pipeline_name/dataset_name may be overridden via the
      source's ``dlt_native`` config block (``DltNativeConfig.pipeline_name`` /
      ``.dataset_name``), defaulting to the source name / ``raw_{name}`` — mirrors
      ``_run_dlt_native_source()``.
    - All other dlt-backed sources (registry-based, e.g. facebook_ads, stripe):
      pipeline_name is always the source name and dataset_name is always
      ``raw_{name}`` — mirrors ``_run_dlt_source()`` / ``_get_dataset_name()``.
      There is no per-source override for these; ``DataSource`` has no top-level
      ``pipeline_name``/``dataset_name`` fields.

    Note: CSV/Local Files sources have no dlt pipeline at all (loaded via
    ``csv_loader.py``, not dlt) — callers should expect no ``_dlt_pipeline_state``
    table to exist for those, regardless of the names returned here.
    """
    if src.dlt_native is not None:
        pipeline_name = src.dlt_native.pipeline_name or src.name
        dataset_name = src.dlt_native.dataset_name or f"raw_{src.name}"
    else:
        pipeline_name = src.name
        dataset_name = f"raw_{src.name}"
    return pipeline_name, dataset_name


def _decode_dlt_state(state_b64: str) -> dict[str, Any]:
    """
    Decode a dlt ``_dlt_pipeline_state.state`` value.

    dlt stores pipeline state as base64(zlib(json)). Verified live against a real
    dlt 1.28.1 sync (dlt_native incremental resource) on 2026-09-09 — see PR
    description for the manual decode used to confirm this.
    """
    import base64
    import json
    import zlib

    decoded: dict[str, Any] = json.loads(zlib.decompress(base64.b64decode(state_b64)))
    return decoded


def _connect_readonly_with_retry(db_path: "Path") -> Any:
    """Open a read-only DuckDB connection, retrying briefly on IOException.

    DuckDB is single-writer (see VAL-003 finding); a concurrent sync can
    transiently hold the write lock even though read-only connections are
    normally allowed alongside a writer. Mirrors the identical retry in
    ``dango/cli/commands/mcp_helpers.py``'s ``_connect_readonly_with_retry()``
    and ``dango/web/routes/query.py``'s ``_execute_query()`` — this codebase's
    established pattern is to duplicate this small retry locally in each
    module that needs it rather than share a central helper (see
    mcp_helpers.py's own docstring on this).
    """
    import time

    import duckdb

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return duckdb.connect(str(db_path), config={"access_mode": "read_only"})
        except duckdb.IOException as e:
            last_exc = e
            if attempt < 2:
                time.sleep(0.1 * (2**attempt))
    assert last_exc is not None
    raise last_exc


def _iter_incremental_cursors(
    state: dict[str, Any],
) -> Iterator[tuple[str, str, str | None, dict[str, Any] | None]]:
    """
    Yield (dlt_source_name, resource_name, cursor_field, cursor_info) tuples for
    every incremental cursor found in a decoded dlt pipeline state.

    Note: the "source" key inside dlt's state dict is the dlt *source function's*
    internal name (schema name), which is not necessarily equal to the Dango
    source name used on the CLI — resources are nested under it regardless.
    """
    sources = state.get("sources") or {}
    for dlt_source_name, source_state in sources.items():
        if not isinstance(source_state, dict):
            continue
        resources = source_state.get("resources") or {}
        for resource_name, resource_state in resources.items():
            if not isinstance(resource_state, dict):
                continue
            incremental = resource_state.get("incremental")
            if not incremental:
                yield dlt_source_name, resource_name, None, None
                continue
            for cursor_field, cursor_info in incremental.items():
                yield dlt_source_name, resource_name, cursor_field, cursor_info


@source.command("inspect-state")
@click.argument("source_name")
@click.pass_context
def source_inspect_state(ctx: click.Context, source_name: str) -> None:
    """
    Show dlt's internal incremental cursor state for a source (read-only).

    Decodes and displays the incremental last_value(s) dlt is tracking for each
    resource of SOURCE_NAME, plus whether a local dlt pipeline cache exists on
    disk. This surfaces information that otherwise requires manually decoding
    base64+zlib+JSON from the _dlt_pipeline_state table by hand — useful when
    diagnosing why an incremental sync is fetching less data than expected.

    This command is strictly read-only: it never modifies, clears, or resets
    any state. Use 'dango sync <name> --full-refresh' to reset state.

    Examples:
      dango source inspect-state stripe
      dango source inspect-state my_dlt_native_source
    """
    import os

    import duckdb
    from rich.table import Table

    from dango.config import get_config

    from ..utils import require_project_context

    console.print(f"🍡 [bold]Inspecting dlt state for: {source_name}[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        config = get_config(project_root)

        src = config.sources.get_source(source_name)
        if not src:
            console.print(f"[red]Error:[/red] Source '{source_name}' not found")
            console.print("\nAvailable sources:")
            for s in config.sources.sources:
                console.print(f"  • {s.name} ({s.type.value})")
            raise click.Abort()

        pipeline_name, dataset_name = _resolve_pipeline_and_dataset_name(src)
        console.print(f"[dim]Pipeline name:[/dim] {pipeline_name}")
        console.print(f"[dim]Dataset name:[/dim]  {dataset_name}\n")

        # --- 1. Destination state (_dlt_pipeline_state table) ---
        duckdb_path = project_root / "data" / "warehouse.duckdb"
        state: dict[str, Any] | None = None
        state_error: str | None = None

        if not duckdb_path.exists():
            state_error = "Warehouse database not found — this source has never been synced."
        else:
            conn = None
            try:
                conn = _connect_readonly_with_retry(duckdb_path)
            except duckdb.IOException as lock_err:
                state_error = (
                    f"Could not open the warehouse read-only (write lock busy): {lock_err}. "
                    "Try again in a moment — a sync or the web server may be writing right now."
                )

            if conn is not None:
                try:
                    table_exists = conn.execute(
                        """
                        SELECT COUNT(*) FROM information_schema.tables
                        WHERE table_schema = ? AND table_name = '_dlt_pipeline_state'
                        """,
                        [dataset_name],
                    ).fetchone()[0]

                    if not table_exists:
                        state_error = (
                            f"No _dlt_pipeline_state table found in schema '{dataset_name}'. "
                            "This is normal for CSV/Local Files sources (no dlt pipeline), or "
                            "a dlt-backed source that has never been synced."
                        )
                    else:
                        row = conn.execute(
                            f'SELECT state FROM "{dataset_name}"._dlt_pipeline_state '
                            "ORDER BY created_at DESC LIMIT 1"
                        ).fetchone()
                        if not row or row[0] is None:
                            state_error = (
                                f"_dlt_pipeline_state table in '{dataset_name}' "
                                "exists but has no rows."
                            )
                        else:
                            try:
                                state = _decode_dlt_state(row[0])
                            except Exception as decode_err:
                                state_error = f"Could not decode state blob: {decode_err}"
                finally:
                    conn.close()

        if state_error:
            console.print(f"[yellow]⚠ {state_error}[/yellow]\n")
        elif state is not None:
            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("dlt source", style="dim")
            table.add_column("Resource", style="white")
            table.add_column("Cursor field", style="dim")
            table.add_column("last_value", style="green")
            table.add_column("initial_value", style="dim")

            found_any = False
            for (
                dlt_source_name,
                resource_name,
                cursor_field,
                cursor_info,
            ) in _iter_incremental_cursors(state):
                found_any = True
                if cursor_field is None:
                    table.add_row(
                        dlt_source_name,
                        resource_name,
                        "[dim]-[/dim]",
                        "[dim]no incremental cursor[/dim]",
                        "[dim]-[/dim]",
                    )
                else:
                    last_value = str(cursor_info.get("last_value", ""))
                    initial_value = str(cursor_info.get("initial_value", ""))
                    table.add_row(
                        dlt_source_name, resource_name, cursor_field, last_value, initial_value
                    )

            if found_any:
                console.print(table)
            else:
                console.print("[yellow]⚠ State was found but contains no resources.[/yellow]")
            console.print()

        # --- 2. Local filesystem cache (~/.dlt/pipelines/{pipeline_name}/) ---
        from datetime import datetime
        from pathlib import Path

        dlt_home = os.path.expanduser("~/.dlt")
        local_cache_path = Path(dlt_home) / "pipelines" / pipeline_name
        if local_cache_path.exists():
            mtime = datetime.fromtimestamp(local_cache_path.stat().st_mtime)
            console.print(
                f"[dim]Local pipeline cache:[/dim] [green]exists[/green] at {local_cache_path} "
                f"(last modified {mtime.strftime('%Y-%m-%d %H:%M:%S')})"
            )
            console.print(
                "[dim]  Note: a stale local cache can itself mask state changes after a "
                "--full-refresh — its mere presence is diagnostic.[/dim]"
            )
        else:
            console.print(
                f"[dim]Local pipeline cache:[/dim] [dim]not found[/dim] at {local_cache_path}"
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


@click.command()
@click.argument("source_name", required=False, default=None)
@click.option("--since", help="Start date for incremental loading (YYYY-MM-DD)")
@click.option("--until", help="End date for incremental loading (YYYY-MM-DD)")
@click.option("--backfill", help="Backfill duration (e.g. '7d', '2w', '1m')")
@click.option("--limit", type=int, help="Limit rows per source (dev testing)")
@click.option("--full-refresh", is_flag=True, help="Drop existing data and reload from scratch")
@click.option("--dry-run", is_flag=True, help="Show what would be synced without executing")
@click.option(
    "--allow-schema-changes",
    is_flag=True,
    help="Allow CSV schema changes (add columns, treat missing as NULL)",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--allow-empty-replace", is_flag=True, hidden=True)
@click.pass_context
def sync(
    ctx: click.Context,
    source_name: str | None,
    since: str | None,
    until: str | None,
    backfill: str | None,
    limit: int | None,
    full_refresh: bool,
    dry_run: bool,
    allow_schema_changes: bool,
    yes: bool,
    allow_empty_replace: bool,
) -> None:
    """
    Load data from all sources (or specific source).

    Examples:
      dango sync                               Sync all enabled sources
      dango sync chess                         Sync only 'chess' source
      dango sync --since 2024-01-01            Override start date
      dango sync --backfill 30d                Backfill last 30 days
      dango sync --limit 1000                  Dev mode: limit rows per source
      dango sync --full-refresh                Reset state and reload all data
      dango sync --dry-run                     Preview what would be synced

    This command:
      1. Runs CSV loaders (incremental)
      2. Runs dlt pipelines (API sources)
      3. Optionally runs dbt models (transformations)
    """
    from datetime import datetime, timedelta

    from dango.config import get_config
    from dango.ingestion import run_sync

    from ..utils import check_v01x_project, require_project_context

    check_v01x_project()

    source = source_name

    console.print("🍡 [bold]Syncing data...[/bold]")
    console.print()

    try:
        # Get project context
        project_root = require_project_context(ctx)

        # --- Validate option conflicts (before lock — fast failures) ---
        if backfill and (since or until):
            console.print(
                "[red]Error:[/red] --backfill conflicts with --since/--until. Use one or the other."
            )
            raise click.Abort()

        if limit is not None and limit <= 0:
            console.print("[red]Error:[/red] --limit must be a positive integer.")
            raise click.Abort()

        # --- Resolve dates (before lock — fast failures) ---
        start_date_obj = None
        end_date_obj = None

        if backfill:
            days = _parse_duration(backfill)
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            start_date_obj = today - timedelta(days=days)
            end_date_obj = today

        if since:
            try:
                start_date_obj = datetime.strptime(since, "%Y-%m-%d")
            except ValueError:
                console.print("[red]Error:[/red] Invalid --since date format. Use YYYY-MM-DD")
                raise click.Abort() from None

        if until:
            try:
                end_date_obj = datetime.strptime(until, "%Y-%m-%d")
            except ValueError:
                console.print("[red]Error:[/red] Invalid --until date format. Use YYYY-MM-DD")
                raise click.Abort() from None

        if start_date_obj and end_date_obj and start_date_obj >= end_date_obj:
            console.print("[red]Error:[/red] --since must be before --until.")
            raise click.Abort()

        # Load configuration (before lock — needed for source resolution + first-sync check)
        config = get_config(project_root)

        # Check for unreferenced custom sources
        unreferenced = check_unreferenced_custom_sources(project_root, config.sources)
        if unreferenced:
            console.print(format_unreferenced_sources_warning(unreferenced))

        # Get sources to sync
        if source:
            # Sync specific source
            source_config = config.sources.get_source(source)
            if not source_config:
                console.print(f"[red]Error:[/red] Source '{source}' not found in sources.yml")
                console.print("\nAvailable sources:")
                for s in config.sources.sources:
                    status = "✓ enabled" if s.enabled else "✗ disabled"
                    console.print(f"  • {s.name} ({s.type.value}) - {status}")
                raise click.Abort()

            sources_to_sync = [source_config]
            console.print(f"Syncing source: [bold]{source}[/bold]")
        else:
            # Sync all enabled sources
            sources_to_sync = config.sources.get_enabled_sources()
            if not sources_to_sync:
                console.print("[yellow]No enabled sources found in sources.yml[/yellow]")
                console.print("\nRun 'dango source add' to add a source")
                return

            console.print(f"Syncing {len(sources_to_sync)} enabled source(s)")

        # Guard rail: on first sync, confirm scope
        # Placed before lock so user doesn't wait for lock if they'll cancel.
        if not yes and not dry_run:
            warehouse_path = project_root / "data" / "warehouse.duckdb"
            if not warehouse_path.exists():
                if not safe_confirm(
                    f"This will sync {len(sources_to_sync)} source(s). Continue?",
                    default=True,
                ):
                    raise click.Abort()

        # Note: DbtLock is no longer acquired here. It is now acquired inside
        # dlt_runner.py around pipeline.load() and dbt transforms only.

        # Check git branch (gentle reminder if on main/master)
        from ..utils import check_git_branch_warning

        check_git_branch_warning(project_root)

        if full_refresh:
            console.print("[yellow]⚠️  Full refresh mode: existing data will be dropped[/yellow]")
            if not yes and not safe_confirm(
                "Full refresh will reload all data. Continue?", default=False
            ):
                raise click.Abort()

        if limit:
            console.print(f"[yellow]⚠️  Dev mode: limiting to {limit} rows per source[/yellow]")

        # Warn about source-specific option limitations
        for src in sources_to_sync:
            src_type = src.type.value
            has_date_range = start_date_obj is not None or end_date_obj is not None
            if has_date_range and not _source_supports_date_range(src_type):
                console.print(
                    f"[yellow]⚠️  '{src.name}' ({src_type}) does not support "
                    f"date range filtering — dates will be ignored[/yellow]"
                )
            if limit and src_type == "csv":
                console.print(
                    f"[yellow]⚠️  '{src.name}' (csv) does not support "
                    f"--limit — CSV loads all rows[/yellow]"
                )

        console.print()

        # Dry run mode - show what would be synced without executing
        if dry_run:
            console.print("[bold cyan]Dry run mode - no changes will be made[/bold cyan]\n")
            console.print("Sources that would be synced:")
            for src in sources_to_sync:
                console.print(f"  • {src.name} ({src.type.value})")
                if src.type.value == "csv":
                    console.print(f"    Path: {src.csv.file_path if src.csv else 'N/A'}")
                elif src.type.value == "dlt_native":
                    console.print(
                        f"    Module: {src.dlt_native.source_module if src.dlt_native else 'N/A'}"
                    )
                else:
                    from dango.ingestion.sources.registry import get_source_metadata

                    meta = get_source_metadata(src.type.value)
                    if meta:
                        console.print(f"    dlt source: {meta.get('dlt_package', src.type.value)}")

            console.print()
            console.print("[dim]Options:[/dim]")
            console.print(f"  • Full refresh: {'Yes' if full_refresh else 'No'}")
            since_display = start_date_obj.strftime("%Y-%m-%d") if start_date_obj else "Default"
            until_display = end_date_obj.strftime("%Y-%m-%d") if end_date_obj else "Default"
            console.print(f"  • Since: {since_display}")
            console.print(f"  • Until: {until_display}")
            if limit:
                console.print(f"  • Limit: {limit} rows")
            console.print()
            console.print("[dim]Run without --dry-run to execute sync[/dim]")
            return

        # Pre-sync OAuth token validation
        from dango.exceptions import OAuthTokenExpiredError, OAuthTokenRevokedError
        from dango.oauth.validation import validate_before_sync

        for src in sources_to_sync:
            try:
                validate_before_sync(src.type.value, project_root)
            except (OAuthTokenRevokedError, OAuthTokenExpiredError) as oauth_err:
                console.print(f"\n[red]{oauth_err.user_message}[/red]")
                raise click.Abort() from oauth_err

        # Run sync
        try:
            summary = run_sync(
                project_root=project_root,
                sources=sources_to_sync,
                start_date=start_date_obj,
                end_date=end_date_obj,
                full_refresh=full_refresh,
                limit=limit,
                allow_schema_changes=allow_schema_changes,
                allow_empty_replace=allow_empty_replace,
            )
        except KeyboardInterrupt:
            console.print("\n[yellow]Sync interrupted — progress saved.[/yellow]")
            console.print("[green]Resume with the same command.[/green]")
            return

        # Trigger Metabase schema sync (if Metabase is running).
        # Metabase is restarted by _load_with_lock() after the write phase,
        # so it is available for schema sync on both local and cloud.
        if summary["failed_count"] == 0:
            console.print()
            console.print("[dim]Updating Metabase schema...[/dim]")
            from dango.visualization.metabase import sync_metabase_schema

            if sync_metabase_schema(project_root):
                console.print("[green]✓[/green] Metabase schema updated")
            else:
                # Silent skip if Metabase isn't configured or running
                console.print(
                    "[dim]ℹ Metabase not running (schema will sync automatically when started)[/dim]"
                )

        # Display OAuth warnings at the very end (so users don't miss them)
        oauth_warnings = summary.get("oauth_warnings", [])
        if oauth_warnings:
            console.print()
            console.print("[yellow]" + "=" * 60 + "[/yellow]")
            console.print("[yellow]⚠️  OAuth Token Warnings:[/yellow]")
            console.print("[yellow]" + "=" * 60 + "[/yellow]")
            for warning in oauth_warnings:
                console.print(
                    f"  • {warning['source_name']}: expires in {warning['days_left']} day(s) ({warning['expires_at']})"
                )
                console.print(
                    f"    [cyan]Re-authenticate:[/cyan] dango oauth {warning['source_type']}"
                )
            console.print()

        # Exit with error code if any sources failed
        if summary["failed_count"] > 0:
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e
