"""dango/cli/commands/data.py

Database management commands (db status, db clean) and project validation.
"""

import click

from dango.cli import console


@click.group()
@click.pass_context
def db(ctx: click.Context) -> None:
    """
    Manage DuckDB database.

    Commands:
      dango db status     Show database status and orphaned tables
      dango db clean      Remove orphaned tables
    """
    pass


@db.command("status")
@click.pass_context
def db_status(ctx: click.Context) -> None:
    """
    Show database status including orphaned tables.

    Orphaned tables are tables that exist in DuckDB but have no corresponding
    source configuration in .dango/sources.yml
    """
    import duckdb
    from rich.table import Table

    from dango.config import get_config

    from ..utils import require_project_context

    console.print("🍡 [bold]Database Status[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        config = get_config(project_root)
        duckdb_path = project_root / "data" / "warehouse.duckdb"

        if not duckdb_path.exists():
            console.print("[yellow]⚠️  No database found[/yellow]")
            console.print("Run 'dango sync' to create the database")
            return

        # Connect to database
        conn = duckdb.connect(str(duckdb_path), read_only=True)

        # Get all tables from relevant schemas (without row counts first)
        # Include: raw, raw_*, staging, intermediate, marts
        tables = conn.execute("""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema IN ('raw', 'staging', 'intermediate', 'marts')
               OR table_schema LIKE 'raw_%'
            ORDER BY table_schema, table_name
        """).fetchall()

        # Get row counts for each table individually
        result = []
        for schema, table in tables:
            try:
                row = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()
                count = row[0] if row else 0
                result.append((schema, table, f"{count:,} rows"))
            except Exception:
                result.append((schema, table, "Error"))

        # Build schema-to-table mapping from source configurations
        from ..db_helpers import build_schema_table_mapping, is_table_configured

        schema_to_tables, source_to_schema = build_schema_table_mapping(config)

        # Build actual raw tables mapping from database
        # This is used to validate that staging tables have corresponding raw tables
        actual_raw_tables: dict[str, set[str]] = {}
        for schema, table, _size in result:
            if schema.startswith("raw_") and not table.startswith("_dlt_"):
                if schema not in actual_raw_tables:
                    actual_raw_tables[schema] = set()
                actual_raw_tables[schema].add(table)

        configured_tables = []
        orphaned_tables = []

        for schema, table, size in result:
            if is_table_configured(
                schema, table, schema_to_tables, source_to_schema, actual_raw_tables
            ):
                if not table.startswith("_dlt_"):
                    configured_tables.append((schema, table, size, "✅"))
            else:
                orphaned_tables.append((schema, table, size))

        conn.close()

        # Display configured tables
        if configured_tables:
            table = Table(title="Configured Tables", show_header=True, header_style="bold cyan")
            table.add_column("Schema", style="cyan")
            table.add_column("Table", style="white")
            table.add_column("Size", justify="right")
            table.add_column("Status", justify="center")

            for schema, name, size, status in configured_tables:
                table.add_row(schema, name, size, status)

            console.print(table)
            console.print()

        # Display orphaned tables
        if orphaned_tables:
            table = Table(title="Orphaned Tables", show_header=True, header_style="bold yellow")
            table.add_column("Schema", style="yellow")
            table.add_column("Table", style="white")
            table.add_column("Size", justify="right")
            table.add_column("Status", justify="center")

            for schema, name, size in orphaned_tables:
                table.add_row(schema, name, size, "⚠️")

            console.print(table)
            console.print()
            console.print("[yellow]⚠️  Found orphaned tables (no source config)[/yellow]")
            console.print("Run 'dango db clean' to remove them")
        else:
            console.print("[green]✅ No orphaned tables found[/green]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@db.command("clean")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def db_clean(ctx: click.Context, yes: bool) -> None:
    """
    Remove orphaned tables from DuckDB.

    Orphaned tables are tables that exist in DuckDB but have no corresponding
    source configuration in .dango/sources.yml

    Examples:
      dango db clean          Remove orphaned tables (with confirmation)
      dango db clean --yes    Remove without confirmation
    """
    import duckdb
    from rich.prompt import Confirm

    from dango.config import get_config

    from ..utils import require_project_context

    console.print("🍡 [bold]Clean Database[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        config = get_config(project_root)
        duckdb_path = project_root / "data" / "warehouse.duckdb"

        if not duckdb_path.exists():
            console.print("[yellow]⚠️  No database found[/yellow]")
            return

        # Connect to database
        conn = duckdb.connect(str(duckdb_path))

        # Get all tables from relevant schemas (without row counts first)
        # Include: raw, raw_*, staging, intermediate, marts
        tables = conn.execute("""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema IN ('raw', 'staging', 'intermediate', 'marts')
               OR table_schema LIKE 'raw_%'
            ORDER BY table_schema, table_name
        """).fetchall()

        # Get row counts for each table individually
        result = []
        for schema, table in tables:
            try:
                row = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()
                count = row[0] if row else 0
                result.append((schema, table, f"{count:,} rows"))
            except Exception:
                result.append((schema, table, "Error"))

        # Build schema-to-table mapping from source configurations
        from ..db_helpers import build_schema_table_mapping, is_table_configured

        schema_to_tables, source_to_schema = build_schema_table_mapping(config)

        # Build actual raw tables mapping from database
        # This is used to validate that staging tables have corresponding raw tables
        actual_raw_tables: dict[str, set[str]] = {}
        for schema, table, _size in result:
            if schema.startswith("raw_") and not table.startswith("_dlt_"):
                if schema not in actual_raw_tables:
                    actual_raw_tables[schema] = set()
                actual_raw_tables[schema].add(table)

        # Find orphaned tables
        orphaned_tables = []

        for schema, table, size in result:
            if not is_table_configured(
                schema, table, schema_to_tables, source_to_schema, actual_raw_tables
            ):
                orphaned_tables.append((schema, table, size))
            # Note: We do NOT clean intermediate or marts tables
            # These are custom models created by users with dango model add
            # and should not be automatically deleted

        if not orphaned_tables:
            console.print("[green]✅ No orphaned tables found[/green]")
            conn.close()
            return

        # Show orphaned tables
        console.print(f"[yellow]Found {len(orphaned_tables)} orphaned table(s):[/yellow]\n")
        for schema, table, size in orphaned_tables:
            console.print(f"  • {schema}.{table} ({size})")

        console.print()

        # Confirm deletion
        if not yes:
            if not Confirm.ask("Remove these orphaned tables?"):
                console.print("[yellow]Cancelled[/yellow]")
                conn.close()
                return

        # Drop orphaned tables
        dropped_count = 0
        orphaned_sources = set()  # Track orphaned source names for metadata cleanup

        for schema, table, _size in orphaned_tables:
            try:
                conn.execute(f"DROP TABLE IF EXISTS {schema}.{table}")
                console.print(f"[green]✓[/green] Dropped {schema}.{table}")
                dropped_count += 1

                # Track orphaned source name
                if schema == "raw":
                    # Single-resource source: table name is source name
                    orphaned_sources.add(table)
                elif schema.startswith("raw_"):
                    # Multi-resource source: schema name contains source name (raw_sourcename)
                    source_name = schema[4:]  # Remove 'raw_' prefix
                    orphaned_sources.add(source_name)

            except Exception as e:
                console.print(f"[red]✗[/red] Failed to drop {schema}.{table}: {e}")
                from dango.exceptions import is_debug_mode

                if is_debug_mode():
                    import traceback

                    console.print(traceback.format_exc())

        # Clean up metadata for orphaned sources (only if metadata table exists)
        if orphaned_sources:
            # Check if metadata table exists (only created for CSV sources)
            _row = conn.execute("""
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_name = '_dango_file_metadata'
                """).fetchone()
            metadata_table_exists = (_row[0] if _row else 0) > 0

            if metadata_table_exists:
                console.print()
                console.print("[dim]Cleaning metadata...[/dim]")

                metadata_cleaned = 0
                for source_name in orphaned_sources:
                    try:
                        # Count entries first
                        _row = conn.execute(
                            """
                            SELECT COUNT(*) FROM _dango_file_metadata
                            WHERE source_name = ?
                        """,
                            [source_name],
                        ).fetchone()
                        count = _row[0] if _row else 0

                        if count > 0:
                            # Delete entries
                            conn.execute(
                                """
                                DELETE FROM _dango_file_metadata
                                WHERE source_name = ?
                            """,
                                [source_name],
                            )

                            console.print(
                                f"[green]✓[/green] Cleaned metadata for '{source_name}' ({count} entries)"
                            )
                            metadata_cleaned += 1
                    except Exception as e:
                        console.print(
                            f"[yellow]⚠[/yellow] Could not clean metadata for '{source_name}': {e}"
                        )
                        from dango.exceptions import is_debug_mode

                        if is_debug_mode():
                            import traceback

                            console.print(traceback.format_exc())

        conn.close()

        console.print()
        console.print(
            f"[green]✅ Removed {dropped_count}/{len(orphaned_tables)} orphaned table(s)[/green]"
        )

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@click.command("validate")
@click.pass_context
def validate(ctx: click.Context) -> None:
    """
    Validate project configuration and setup.

    This command checks:
    - Project directory structure
    - Configuration files (project.yml, sources.yml)
    - Data source configurations
    - dbt setup (dbt_project.yml, profiles.yml, models)
    - Database connectivity (DuckDB)
    - Required dependencies (dlt, dbt, duckdb, etc.)
    - File permissions

    Run this command to ensure your project is properly configured
    before syncing data or running transformations.

    Examples:
      dango validate    Run all validation checks
    """
    from ..utils import require_project_context
    from ..validate import validate_project

    try:
        project_root = require_project_context(ctx)
        summary = validate_project(project_root)

        # Exit with error code if validation failed
        if not summary["is_valid"]:
            raise SystemExit(1)

    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled[/yellow]")
        raise click.Abort() from None
    except SystemExit:
        raise  # Re-raise SystemExit
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e
