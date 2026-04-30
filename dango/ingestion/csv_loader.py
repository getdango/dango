"""dango/ingestion/csv_loader.py

Incremental file loading with metadata tracking.

Supports CSV, JSON, JSONL, and Parquet formats via DuckDB's native readers.
"""

import glob
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
from rich.console import Console

from dango.config.models import CSVSourceConfig
from dango.exceptions import CSVSchemaMismatchError

console = Console()

# DuckDB read functions keyed by file extension
SUPPORTED_READ_FUNCTIONS: dict[str, str] = {
    ".csv": "read_csv_auto",
    ".json": "read_json_auto",
    ".jsonl": "read_json_auto",
    ".ndjson": "read_json_auto",
    ".parquet": "read_parquet",
}


class CSVLoader:
    """
    Incremental CSV loader with metadata tracking

    Handles:
    - Incremental file loading (only process changed files)
    - File classification (new/updated/unchanged/deleted)
    - Transactional safety (load-first-then-delete pattern)
    - Metadata tracking (which files loaded when)
    """

    def __init__(self, project_root: Path, duckdb_path: Path):
        """
        Initialize CSV loader

        Args:
            project_root: Path to project root directory
            duckdb_path: Path to DuckDB database file
        """
        self.project_root = project_root
        self.duckdb_path = duckdb_path

    def _get_read_function(self, filepath: str) -> str:
        """Get DuckDB read function for file format based on extension."""
        ext = Path(filepath).suffix.lower()
        if ext not in SUPPORTED_READ_FUNCTIONS:
            supported = ", ".join(sorted(SUPPORTED_READ_FUNCTIONS.keys()))
            raise ValueError(f"Unsupported file format '{ext}'. Supported extensions: {supported}")
        return SUPPORTED_READ_FUNCTIONS[ext]

    def load(
        self,
        source_name: str,
        config: CSVSourceConfig,
        target_schema: str = "raw",
        allow_schema_changes: bool = False,
    ) -> dict[str, Any]:
        """
        Load CSV files incrementally

        Args:
            source_name: Name of the source (used as table prefix)
            config: CSV source configuration
            target_schema: Target schema name (default: 'raw')
            allow_schema_changes: If True, allow schema evolution (add cols, NULL missing)

        Returns:
            Dictionary with load statistics
        """
        console.print(f"📄 Loading file source: {source_name}")

        # Resolve directory path (relative to project root if not absolute)
        directory = config.directory
        if not directory.is_absolute():
            directory = self.project_root / directory

        if not directory.exists():
            raise FileNotFoundError(f"CSV directory not found: {directory}")

        # Get current files matching pattern
        pattern = str(directory / config.file_pattern)
        all_matched_files = sorted(glob.glob(pattern))

        # Filter to supported formats only
        current_files = []
        for f in all_matched_files:
            ext = Path(f).suffix.lower()
            if ext in SUPPORTED_READ_FUNCTIONS:
                current_files.append(f)
            elif ext:  # Skip directories (no extension)
                console.print(f"  ⚠️  Skipping unsupported format: {Path(f).name}")

        # Connect to DuckDB (needed even if no files exist, to process deletions)
        conn = duckdb.connect(str(self.duckdb_path))

        # Create schema
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {target_schema}")

        # Setup metadata tracking
        self._setup_metadata_table(conn)

        # Show file count
        if not current_files:
            console.print(f"  ⚠️  No files found matching: {pattern}")
        else:
            console.print(f"  Found {len(current_files)} file(s)")

        # Classify files
        classified = self._classify_files(conn, source_name, current_files)

        console.print(
            f"  New: {len(classified['new'])} | "
            f"Updated: {len(classified['updated'])} | "
            f"Unchanged: {len(classified['unchanged'])} | "
            f"Deleted: {len(classified['deleted'])}"
        )

        # Target table name
        # Use source_name as the table name within raw_{source_name} schema
        # This follows industry practice (Fivetran, Airbyte) where table name = resource name
        # Result: raw_test_csv_1.test_csv_1 (redundant but clear when schema not shown)
        table_name = source_name
        target_table = f"{target_schema}.{table_name}"

        # Clean up legacy "data" table if it exists (from old CSV loader behavior)
        # This prevents orphaned tables when migrating from old naming scheme
        if table_name != "data":
            legacy_table_exists = self._check_table_exists(conn, target_schema, "data")
            if legacy_table_exists:
                console.print("  [dim]Cleaning up legacy 'data' table...[/dim]")
                conn.execute(f'DROP TABLE IF EXISTS "{target_schema}"."data"')

        # Create table on first run
        table_exists = self._check_table_exists(conn, target_schema, table_name)

        # PRE-VALIDATE: Check ALL file schemas match before loading ANY data
        # This prevents partial data loading when schema mismatches exist
        files_to_validate = classified["new"] + classified["updated"]
        evolution_columns: dict[str, str] = {}  # new columns to add if evolving
        if files_to_validate:
            try:
                evolution_columns = self._validate_all_files_schema_match(
                    conn,
                    files_to_validate,
                    target_table,
                    source_name,
                    table_exists,
                    allow_schema_changes=allow_schema_changes,
                )
            except CSVSchemaMismatchError as e:
                # Schema validation failed - exit immediately without loading anything
                conn.close()
                console.print()
                console.print(f"[red]{str(e)}[/red]")
                console.print()
                return {
                    "status": "error",
                    "error": str(e),
                    "new": 0,
                    "updated": 0,
                    "deleted": 0,
                    "skipped": len(classified["unchanged"]),
                    "total_rows": 0,
                }

        # Apply schema evolution if needed (add new columns before loading)
        if evolution_columns and table_exists:
            self._evolve_table_schema(conn, target_table, evolution_columns)
            console.print(
                f"  [green]Schema evolved: added {len(evolution_columns)} column(s): "
                f"{', '.join(evolution_columns.keys())}[/green]"
            )

        # Note: We don't drop the table when all files are deleted
        # Instead, we let the file deletion logic below handle it by deleting rows
        # This keeps the table schema intact and allows dbt models to run (producing 0 rows)

        if not table_exists and (classified["new"] or classified["updated"]):
            first_file = (classified["new"] + classified["updated"])[0][0]
            self._create_table_from_file(conn, first_file, target_table, source_name)

        # Process files
        stats = {
            "new": 0,
            "updated": 0,
            "deleted": 0,
            "skipped": len(classified["unchanged"]),
            "total_rows": 0,
        }

        try:
            # Load new files
            for filepath, _metadata in classified["new"]:
                if self._load_new_file(conn, filepath, target_table, source_name):
                    stats["new"] += 1

            # Reload updated files
            for filepath, _metadata in classified["updated"]:
                if self._reload_updated_file(conn, filepath, target_table, source_name):
                    stats["updated"] += 1

            # Delete removed files
            for filepath in classified["deleted"]:
                if self._delete_file_data(conn, filepath, target_table, source_name):
                    stats["deleted"] += 1

            # Get final row count (only if table exists)
            table_exists = self._check_table_exists(conn, target_schema, table_name)
            if table_exists:
                stats["total_rows"] = conn.execute(
                    f"SELECT COUNT(*) FROM {target_table}"
                ).fetchone()[0]
            else:
                stats["total_rows"] = 0

            conn.close()

            console.print(
                f"  ✓ Loaded: {stats['new']} new, {stats['updated']} updated, "
                f"{stats['deleted']} deleted | Total rows: {stats['total_rows']}"
            )

            return {
                "status": "success",
                **stats,
            }

        except CSVSchemaMismatchError as e:
            # Schema mismatch - fail immediately with clear error
            conn.close()
            console.print()
            console.print(f"[red]{str(e)}[/red]")
            console.print()
            return {
                "status": "error",
                "error": str(e),
                **stats,
            }

    def _setup_metadata_table(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Create metadata tracking table if not exists"""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _dango_file_metadata (
                source_name VARCHAR,
                file_path VARCHAR,
                file_size BIGINT,
                file_mtime TIMESTAMP,
                rows_loaded BIGINT,
                status VARCHAR,
                loaded_at TIMESTAMP,
                error_message VARCHAR,
                PRIMARY KEY (source_name, file_path)
            )
        """)

    def _get_file_metadata(self, filepath: str) -> dict[str, Any]:
        """Get file size and modification time"""
        stat = os.stat(filepath)
        return {"size": stat.st_size, "mtime": datetime.fromtimestamp(stat.st_mtime)}

    def _classify_files(
        self, conn: duckdb.DuckDBPyConnection, source_name: str, current_files: list[str]
    ) -> dict[str, list]:
        """Classify files into: new, updated, unchanged, deleted"""
        # Get previously loaded files from metadata
        prev_files = {}
        result = conn.execute(
            """
            SELECT file_path, file_mtime
            FROM _dango_file_metadata
            WHERE source_name = ?
        """,
            [source_name],
        ).fetchall()

        for file_path, mtime in result:
            prev_files[file_path] = mtime

        new_files = []
        updated_files = []
        unchanged_files = []

        for filepath in current_files:
            metadata = self._get_file_metadata(filepath)

            if filepath not in prev_files:
                new_files.append((filepath, metadata))
            elif metadata["mtime"] > prev_files[filepath]:
                updated_files.append((filepath, metadata))
            else:
                unchanged_files.append((filepath, metadata))

        # Find deleted files
        current_file_set = set(current_files)
        deleted_files = [f for f in prev_files.keys() if f not in current_file_set]

        # Debug logging
        console.print("[dim]  📊 File classification:[/dim]")
        console.print(f"[dim]     Current files on disk: {len(current_files)}[/dim]")
        console.print(f"[dim]     Files in metadata: {len(prev_files)}[/dim]")
        console.print(
            f"[dim]     New: {len(new_files)}, Updated: {len(updated_files)}, Unchanged: {len(unchanged_files)}, Deleted: {len(deleted_files)}[/dim]"
        )
        if deleted_files:
            console.print("[yellow]  🗑️  Deleted files detected:[/yellow]")
            for f in deleted_files:
                console.print(f"[yellow]     - {f}[/yellow]")

        return {
            "new": new_files,
            "updated": updated_files,
            "unchanged": unchanged_files,
            "deleted": deleted_files,
        }

    def _check_table_exists(self, conn: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
        """Check if table exists"""
        result = conn.execute(
            f"""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = '{schema}' AND table_name = '{table}'
        """
        ).fetchone()[0]
        return result > 0

    def _get_file_columns(self, conn: duckdb.DuckDBPyConnection, filepath: str) -> list[str]:
        """
        Get column names from a data file (CSV, JSON, JSONL, or Parquet).

        Args:
            conn: DuckDB connection
            filepath: Path to data file

        Returns:
            List of column names (excluding metadata columns)
        """
        read_fn = self._get_read_function(filepath)
        temp_view = f"_temp_schema_check_{int(datetime.now().timestamp())}"
        try:
            conn.execute(
                f"CREATE TEMP VIEW {temp_view} AS SELECT * FROM {read_fn}('{filepath}') LIMIT 0"
            )
            columns = [col[0] for col in conn.execute(f"DESCRIBE {temp_view}").fetchall()]
            conn.execute(f"DROP VIEW {temp_view}")
            return columns
        except Exception as e:
            # Clean up view if it exists
            try:
                conn.execute(f"DROP VIEW IF EXISTS {temp_view}")
            except Exception:
                pass
            raise Exception(f"Failed to read file schema from {filepath}: {e}") from e

    def _get_table_columns(self, conn: duckdb.DuckDBPyConnection, table_name: str) -> list[str]:
        """
        Get column names from an existing table (excluding metadata columns).

        Args:
            conn: DuckDB connection
            table_name: Fully qualified table name (schema.table)

        Returns:
            List of data column names (excludes _dango_* columns)
        """
        columns = [
            col[0]
            for col in conn.execute(f"DESCRIBE {table_name}").fetchall()
            if not col[0].startswith("_dango_")
        ]
        return columns

    def _get_file_column_types(
        self, conn: duckdb.DuckDBPyConnection, filepath: str
    ) -> dict[str, str]:
        """Get column names and types from a data file.

        Args:
            conn: DuckDB connection
            filepath: Path to data file

        Returns:
            Mapping of {column_name: data_type}
        """
        read_fn = self._get_read_function(filepath)
        temp_view = f"_temp_types_check_{int(datetime.now().timestamp())}"
        try:
            conn.execute(
                f"CREATE TEMP VIEW {temp_view} AS SELECT * FROM {read_fn}('{filepath}') LIMIT 0"
            )
            rows = conn.execute(f"DESCRIBE {temp_view}").fetchall()
            conn.execute(f"DROP VIEW {temp_view}")
            return {row[0]: row[1] for row in rows}
        except Exception:
            try:
                conn.execute(f"DROP VIEW IF EXISTS {temp_view}")
            except Exception:
                pass
            return {}

    def _evolve_table_schema(
        self,
        conn: duckdb.DuckDBPyConnection,
        target_table: str,
        new_columns: dict[str, str],
    ) -> None:
        """Add new columns to an existing table for schema evolution.

        Args:
            conn: DuckDB connection
            target_table: Fully qualified table name (schema.table)
            new_columns: Mapping of {column_name: column_type} to add
        """
        for col_name, col_type in new_columns.items():
            conn.execute(f'ALTER TABLE {target_table} ADD COLUMN "{col_name}" {col_type}')

    def _validate_all_files_schema_match(
        self,
        conn: duckdb.DuckDBPyConnection,
        files: list[tuple],  # List of (filepath, metadata) tuples
        target_table: str,
        source_name: str,
        table_exists: bool,
        allow_schema_changes: bool = False,
    ) -> dict[str, str]:
        """
        Pre-validate ALL CSV files have matching schemas before loading ANY data.

        For first load (no table): Validates all files have identical schemas.
        For incremental (table exists): Validates all files match existing table schema.

        When ``allow_schema_changes=True`` and table exists, extra columns in files
        are collected for ALTER TABLE, and missing columns are allowed (NULLs).

        Args:
            conn: DuckDB connection
            files: List of (filepath, metadata) tuples to validate
            target_table: Target table name (schema.table)
            source_name: Source name (for error messages)
            table_exists: Whether target table already exists
            allow_schema_changes: If True, tolerate schema differences

        Returns:
            Dict of {column_name: column_type} for new columns to add (empty if none).

        Raises:
            CSVSchemaMismatchError: If any schema mismatch is found (strict mode)
        """
        if not files:
            return {}

        # Extract just file paths
        filepaths = [f[0] for f in files]

        # Get schema from first file (or existing table)
        if table_exists:
            # Incremental load: all files must match existing table
            reference_columns = self._get_table_columns(conn, target_table)
            reference_name = "existing table"
        else:
            # First load: all files must match first file
            reference_columns = self._get_file_columns(conn, filepaths[0])
            reference_name = os.path.basename(filepaths[0])

        reference_set = set(reference_columns)

        # Validate each file against reference schema
        mismatched_files = []
        # Track all new columns across files (for schema evolution)
        all_new_columns: dict[str, str] = {}

        for filepath in filepaths:
            filename = os.path.basename(filepath)

            # Skip reference file for first load
            if not table_exists and filepath == filepaths[0]:
                continue

            try:
                file_columns = self._get_file_columns(conn, filepath)
                file_set = set(file_columns)

                if file_set != reference_set:
                    new_columns = file_set - reference_set
                    missing_columns = reference_set - file_set

                    if allow_schema_changes and table_exists:
                        # Collect new columns with their types for evolution
                        if new_columns:
                            file_types = self._get_file_column_types(conn, filepath)
                            for col in new_columns:
                                if col in file_types:
                                    all_new_columns[col] = file_types[col]
                        # Missing columns are allowed (will be NULL)
                        # Don't add to mismatched_files
                    else:
                        mismatched_files.append(
                            {
                                "filename": filename,
                                "columns": file_columns,
                                "new_columns": new_columns,
                                "missing_columns": missing_columns,
                            }
                        )
            except Exception as e:
                # If we can't read the file schema, treat as mismatch
                mismatched_files.append({"filename": filename, "error": str(e)})

        # If any mismatches found, build detailed error message
        if mismatched_files:
            error_lines = ["❌ Schema validation failed", "", f"{'=' * 60}", ""]

            if table_exists:
                error_lines.append("Validating files against existing table schema:")
            else:
                error_lines.append(f"Validating {len(filepaths)} file(s) for consistent schema:")

            error_lines.append("")

            # Show reference schema
            error_lines.append(f"Reference schema ({reference_name}):")
            error_lines.append(f"  {sorted(reference_columns)}")
            error_lines.append("")

            # Show mismatched files
            error_lines.append(f"Files with schema mismatches ({len(mismatched_files)}):")
            error_lines.append("")

            for mismatch in mismatched_files:
                if "error" in mismatch:
                    error_lines.append(f"  ✗ {mismatch['filename']}")
                    error_lines.append(f"    Error reading file: {mismatch['error']}")
                else:
                    error_lines.append(f"  ✗ {mismatch['filename']}")
                    if mismatch["new_columns"]:
                        error_lines.append(f"    Extra columns: {sorted(mismatch['new_columns'])}")
                    if mismatch["missing_columns"]:
                        error_lines.append(
                            f"    Missing columns: {sorted(mismatch['missing_columns'])}"
                        )
                error_lines.append("")

            error_lines.extend(
                [
                    f"{'=' * 60}",
                    "",
                    "❌ No data was loaded",
                    "",
                    "To fix:",
                    "  1. Ensure all data files have identical schemas",
                    "  2. OR remove/fix mismatched files",
                    "  3. dango sync",
                    "",
                ]
            )

            if table_exists:
                error_lines.extend(
                    [
                        "If you need to change the table schema:",
                        f"  1. dango source remove {source_name}",
                        "  2. dango db clean",
                        f"  3. dango source add  # Re-add '{source_name}'",
                        "  4. dango sync",
                        "",
                        "Or use --allow-schema-changes to accept schema evolution.",
                    ]
                )

            raise CSVSchemaMismatchError("\n".join(error_lines))

        return all_new_columns

    def _validate_schema_match(
        self, conn: duckdb.DuckDBPyConnection, filepath: str, target_table: str, source_name: str
    ) -> None:
        """
        Validate that CSV file schema matches target table schema.
        Raises CSVSchemaMismatchError if schemas don't match (strict mode).

        Args:
            conn: DuckDB connection
            filepath: Path to CSV file to validate
            target_table: Target table name (schema.table)
            source_name: Source name (for error messages)

        Raises:
            CSVSchemaMismatchError: If schemas don't match
        """
        csv_columns = self._get_file_columns(conn, filepath)
        table_columns = self._get_table_columns(conn, target_table)

        # Compare column sets
        csv_set = set(csv_columns)
        table_set = set(table_columns)

        if csv_set != table_set:
            # Calculate differences
            new_columns = csv_set - table_set
            missing_columns = table_set - csv_set

            # Build clear error message
            filename = os.path.basename(filepath)
            error_lines = [
                f"❌ Schema mismatch detected in '{filename}'",
                "",
                f"Expected columns: {sorted(table_columns)}",
                f"File has columns: {sorted(csv_columns)}",
                "",
            ]

            if new_columns:
                error_lines.append(f"New columns in file: {sorted(new_columns)}")
            if missing_columns:
                error_lines.append(f"Missing columns in file: {sorted(missing_columns)}")

            error_lines.extend(
                [
                    "",
                    "To update the table schema:",
                    f"  1. dango source remove {source_name}",
                    "  2. dango db clean",
                    f"  3. dango source add  # Re-add '{source_name}'",
                    "  4. dango sync",
                    "",
                    "Note: Your CSV files in the folder will NOT be deleted.",
                    "      Just re-add the source pointing to the same folder.",
                ]
            )

            raise CSVSchemaMismatchError("\n".join(error_lines))

    def _create_table_from_file(
        self,
        conn: duckdb.DuckDBPyConnection,
        filepath: str,
        target_table: str,
        source_name: str,
    ) -> None:
        """Create table with schema inferred from first file"""
        temp_table = f"_temp_schema_{int(datetime.now().timestamp())}"
        metadata = self._get_file_metadata(filepath)
        filename = os.path.basename(filepath)
        read_fn = self._get_read_function(filepath)

        # Load to temp table with metadata columns
        conn.execute(f"""
            CREATE TABLE {temp_table} AS
            SELECT
                *,
                '{filename}' AS _dango_filename,
                TIMESTAMP '{metadata["mtime"].strftime("%Y-%m-%d %H:%M:%S")}' AS _dango_file_mtime,
                CURRENT_TIMESTAMP AS _dango_loaded_at,
                false AS _dango_deleted
            FROM {read_fn}('{filepath}')
        """)

        # Create target table from temp (empty)
        conn.execute(f"""
            CREATE TABLE {target_table} AS
            SELECT * FROM {temp_table} WHERE 1=0
        """)

        conn.execute(f"DROP TABLE {temp_table}")

    def _load_new_file(
        self,
        conn: duckdb.DuckDBPyConnection,
        filepath: str,
        target_table: str,
        source_name: str,
    ) -> bool:
        """Load a new file"""
        filename = os.path.basename(filepath)
        temp_table = f"_temp_{source_name}_{int(datetime.now().timestamp())}"

        try:
            # Validate schema matches table (strict mode - fail on any mismatch)
            self._validate_schema_match(conn, filepath, target_table, source_name)

            metadata = self._get_file_metadata(filepath)
            read_fn = self._get_read_function(filepath)

            # Load to temp table
            conn.execute(f"""
                CREATE TABLE {temp_table} AS
                SELECT
                    *,
                    '{filename}' AS _dango_filename,
                    TIMESTAMP '{metadata["mtime"].strftime("%Y-%m-%d %H:%M:%S")}' AS _dango_file_mtime,
                    CURRENT_TIMESTAMP AS _dango_loaded_at,
                    false AS _dango_deleted
                FROM {read_fn}('{filepath}')
            """)

            row_count = conn.execute(f"SELECT COUNT(*) FROM {temp_table}").fetchone()[0]

            if row_count == 0:
                console.print(f"    ⚠️  Skipping {filename} (0 rows)")
                conn.execute(f"DROP TABLE {temp_table}")
                return False

            # Insert into target
            conn.execute(f"INSERT INTO {target_table} SELECT * FROM {temp_table}")

            # Update metadata
            conn.execute(
                """
                INSERT OR REPLACE INTO _dango_file_metadata
                (source_name, file_path, file_size, file_mtime, rows_loaded, status, loaded_at)
                VALUES (?, ?, ?, ?, ?, 'loaded', CURRENT_TIMESTAMP)
            """,
                [source_name, filepath, metadata["size"], metadata["mtime"], row_count],
            )

            conn.execute(f"DROP TABLE {temp_table}")

            console.print(f"    ✓ {filename}: {row_count:,} rows")
            return True

        except CSVSchemaMismatchError:
            # Schema mismatch - re-raise to fail-fast (don't suppress)
            raise
        except Exception as e:
            console.print(f"    ❌ Failed to load {filename}: {e}")
            # Attempt cleanup of temp table
            try:
                conn.execute(f"DROP TABLE IF EXISTS {temp_table}")
            except Exception as cleanup_error:
                # Log cleanup failure (don't hide it)
                console.print(f"    ⚠️  Failed to cleanup temp table {temp_table}: {cleanup_error}")
            return False

    def _reload_updated_file(
        self,
        conn: duckdb.DuckDBPyConnection,
        filepath: str,
        target_table: str,
        source_name: str,
    ) -> bool:
        """Reload an updated file (transactional)"""
        filename = os.path.basename(filepath)
        temp_table = f"_temp_{source_name}_{int(datetime.now().timestamp())}"

        try:
            # Validate schema matches table (strict mode - fail on any mismatch)
            self._validate_schema_match(conn, filepath, target_table, source_name)

            metadata = self._get_file_metadata(filepath)
            read_fn = self._get_read_function(filepath)

            # Load to temp table
            conn.execute(f"""
                CREATE TABLE {temp_table} AS
                SELECT
                    *,
                    '{filename}' AS _dango_filename,
                    TIMESTAMP '{metadata["mtime"].strftime("%Y-%m-%d %H:%M:%S")}' AS _dango_file_mtime,
                    CURRENT_TIMESTAMP AS _dango_loaded_at,
                    false AS _dango_deleted
                FROM {read_fn}('{filepath}')
            """)

            row_count = conn.execute(f"SELECT COUNT(*) FROM {temp_table}").fetchone()[0]

            if row_count == 0:
                console.print(f"    ⚠️  Skipping {filename} (0 rows, keeping old version)")
                conn.execute(f"DROP TABLE {temp_table}")
                return False

            # Atomic swap
            conn.execute("BEGIN TRANSACTION")

            try:
                # Delete old data
                conn.execute(f"DELETE FROM {target_table} WHERE _dango_filename = ?", [filename])

                # Insert new data
                conn.execute(f"INSERT INTO {target_table} SELECT * FROM {temp_table}")

                # Update metadata
                conn.execute(
                    """
                    INSERT OR REPLACE INTO _dango_file_metadata
                    (source_name, file_path, file_size, file_mtime, rows_loaded, status, loaded_at)
                    VALUES (?, ?, ?, ?, ?, 'updated', CURRENT_TIMESTAMP)
                """,
                    [source_name, filepath, metadata["size"], metadata["mtime"], row_count],
                )

                conn.execute("COMMIT")

                console.print(f"    🔄 {filename}: {row_count:,} rows (reloaded)")
                return True

            except Exception as e:
                conn.execute("ROLLBACK")
                raise e

            finally:
                conn.execute(f"DROP TABLE IF EXISTS {temp_table}")

        except CSVSchemaMismatchError:
            # Schema mismatch - re-raise to fail-fast (don't suppress)
            raise
        except Exception as e:
            console.print(f"    ❌ Failed to reload {filename}: {e}")
            return False

    def _delete_file_data(
        self,
        conn: duckdb.DuckDBPyConnection,
        filepath: str,
        target_table: str,
        source_name: str,
    ) -> bool:
        """Remove data from a deleted file"""
        filename = os.path.basename(filepath)

        try:
            conn.execute(f"DELETE FROM {target_table} WHERE _dango_filename = ?", [filename])

            conn.execute(
                """
                UPDATE _dango_file_metadata
                SET status = 'deleted', loaded_at = CURRENT_TIMESTAMP
                WHERE source_name = ? AND file_path = ?
            """,
                [source_name, filepath],
            )

            console.print(f"    🗑️  {filename} (removed)")
            return True

        except Exception as e:
            console.print(f"    ❌ Failed to delete {filename}: {e}")
            return False
