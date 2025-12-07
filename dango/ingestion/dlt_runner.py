"""
Dango dlt Pipeline Runner

Generic runner for all dlt verified sources + custom CSV/REST API sources.

Key features:
- Dynamic source loading (importlib) - no hardcoded source logic
- Automatic DuckDB configuration
- State management for incremental loading
- Full-refresh support
- Error handling with retry logic
- CSV special handling (uses custom CSV loader)
"""

import importlib
import time
import signal
import platform
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List
from rich.console import Console
from dotenv import load_dotenv

import dlt
from dlt.common.pipeline import LoadInfo

from dango.config.models import (
    DataSource,
    SourceType,
    CSVSourceConfig,
    RESTAPISourceConfig,
    DltNativeConfig,
)
from dango.ingestion.csv_loader import CSVLoader
from dango.ingestion.sources.registry import get_source_metadata

console = Console()


class SyncTimeoutError(Exception):
    """Raised when sync exceeds timeout"""
    pass


class DltPipelineRunner:
    """
    Generic pipeline runner for all dlt sources

    Usage:
        runner = DltPipelineRunner(project_root)
        result = runner.run_source(source_config)
    """

    def __init__(self, project_root: Path):
        """
        Initialize pipeline runner

        Args:
            project_root: Path to project root directory
        """
        self.project_root = project_root
        self.duckdb_path = project_root / "data" / "warehouse.duckdb"
        self.duckdb_path.parent.mkdir(parents=True, exist_ok=True)

        # Load credentials with priority: .dlt/ > .env
        self._load_credentials()

    def _load_credentials(self):
        """
        Load credentials from .dlt/ directory and .env file

        Priority order:
        1. .dlt/secrets.toml (highest priority - dlt native)
        2. .env file (fallback for backward compatibility)

        dlt automatically loads .dlt/secrets.toml and .dlt/config.toml,
        but we also load .env for backward compatibility with existing projects.
        """
        # First, load .env file (lower priority)
        env_file = self.project_root / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)  # Don't override existing env vars

        # dlt will automatically load .dlt/secrets.toml and .dlt/config.toml
        # from the current working directory when creating pipelines.
        # We just need to ensure we're running from the project root.
        #
        # Note: dlt uses the current working directory to find .dlt/
        # This is handled in the run_source method by changing directory.

    def _check_dependencies(self, source_type: str) -> tuple:
        """
        Check if source has required pip dependencies installed.

        Args:
            source_type: Source type key (e.g., "google_ads")

        Returns:
            Tuple of (all_installed: bool, missing: list of dep dicts)
        """
        metadata = get_source_metadata(source_type)
        if not metadata:
            return True, []

        pip_deps = metadata.get("pip_dependencies", [])
        if not pip_deps:
            return True, []

        missing = []
        for dep in pip_deps:
            try:
                __import__(dep["import"])
            except ImportError:
                missing.append(dep)

        return len(missing) == 0, missing

    def _run_with_timeout(self, func, timeout_minutes: int, *args, **kwargs):
        """
        Run a function with timeout (Unix-only using signals)

        Args:
            func: Function to execute
            timeout_minutes: Timeout in minutes
            *args, **kwargs: Arguments to pass to function

        Returns:
            Function result

        Raises:
            SyncTimeoutError: If execution exceeds timeout
        """
        # Check if we're on a Unix-like system (signal.alarm not available on Windows)
        is_unix = platform.system() in ['Linux', 'Darwin']  # Darwin = macOS

        if not is_unix:
            # Windows: No timeout (signal.alarm not available)
            console.print(f"[dim]⚠️  Timeout not supported on Windows - running without timeout[/dim]")
            return func(*args, **kwargs)

        def timeout_handler(signum, frame):
            raise SyncTimeoutError(f"Sync exceeded {timeout_minutes} minute timeout")

        # Set up signal handler
        old_handler = signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(timeout_minutes * 60)

        try:
            result = func(*args, **kwargs)
            signal.alarm(0)  # Cancel alarm
            return result
        except SyncTimeoutError:
            console.print(f"[red]⏱️  Sync timeout after {timeout_minutes} minutes[/red]")
            raise
        finally:
            signal.alarm(0)  # Ensure alarm is cancelled
            signal.signal(signal.SIGALRM, old_handler)  # Restore old handler

    def run_source(
        self,
        source_config: DataSource,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        full_refresh: bool = False,
        timeout_minutes: int = 60,
    ) -> Dict[str, Any]:
        """
        Run data pipeline for any source type

        Args:
            source_config: Source configuration (from sources.yml)
            start_date: Override start date for incremental loading
            end_date: Override end date for incremental loading
            full_refresh: Drop existing data and reload from scratch
            timeout_minutes: Timeout in minutes (default: 60)

        Returns:
            Dictionary with load statistics and status
        """
        from dango.utils.activity_log import log_activity
        from dango.utils.sync_history import save_sync_history_entry
        from dango.utils.db_health import check_disk_space, check_duckdb_health, DiskSpaceError

        source_name = source_config.name
        source_type = source_config.type
        start_time = datetime.now()

        console.print(f"\n{'='*60}")
        console.print(f"🍡 Syncing: [bold]{source_name}[/bold] ({source_type.value})")
        console.print(f"{'='*60}")

        # Check pip dependencies before sync
        deps_ok, missing_deps = self._check_dependencies(source_type.value)
        if not deps_ok:
            # Add missing to requirements.txt (avoid duplicates)
            req_file = self.project_root / "requirements.txt"
            existing = set()
            if req_file.exists():
                existing = {line.strip() for line in req_file.read_text().split("\n") if line.strip()}

            new_deps = [d["pip"] for d in missing_deps if d["pip"] not in existing]
            if new_deps:
                with open(req_file, "a") as f:
                    for dep in new_deps:
                        f.write(f"{dep}\n")
                console.print(f"\n[green]✓ Added to requirements.txt: {', '.join(new_deps)}[/green]")

            # Show error and instructions
            error_message = f"Missing required dependencies: {', '.join(d['pip'] for d in missing_deps)}"
            console.print(f"\n[red]❌ {error_message}[/red]")
            console.print(f"\n[bold]To fix, run:[/bold]")
            console.print(f"  [cyan]pip install -r requirements.txt[/cyan]")
            console.print(f"\nThen retry: [cyan]dango sync --source {source_name}[/cyan]\n")

            log_activity(
                project_root=self.project_root,
                level="error",
                source=source_name,
                message=f"Sync blocked: {error_message}"
            )

            return {
                "status": "failed",
                "source": source_name,
                "error": error_message,
                "rows_loaded": 0,
            }

        # Check disk space before starting sync
        try:
            check_disk_space(self.project_root, min_free_gb=5)
        except DiskSpaceError as e:
            error_message = str(e)
            console.print(f"[red]❌ {error_message}[/red]")

            # Save failed sync history
            history_entry = {
                "timestamp": start_time.isoformat(),
                "status": "failed",
                "duration_seconds": 0,
                "rows_processed": 0,
                "full_refresh": full_refresh,
                "error_message": error_message
            }
            save_sync_history_entry(self.project_root, source_name, history_entry)

            log_activity(
                project_root=self.project_root,
                level="error",
                source=source_name,
                message=f"Sync blocked: {error_message}"
            )

            return {
                "status": "failed",
                "source": source_name,
                "error": error_message,
                "rows_loaded": 0,
            }

        # Check DuckDB health and log warnings
        try:
            db_health = check_duckdb_health(self.duckdb_path)
            if db_health['status'] == 'large':
                console.print(f"[yellow]⚠️  Database is large ({db_health['size_gb']}GB) - consider archiving old data[/yellow]")
            elif db_health['status'] == 'critical':
                console.print(f"[yellow]⚠️  Database is very large ({db_health['size_gb']}GB) - performance may be affected[/yellow]")
        except Exception as e:
            console.print(f"[dim]⚠️  Could not check database health: {e}[/dim]")

        # Log sync start
        log_activity(
            project_root=self.project_root,
            level="info",
            source=source_name,
            message=f"Starting sync"
        )

        try:
            # CSV: Custom implementation (Phase 1 loader)
            if source_type == SourceType.CSV:
                result = self._run_csv_source(source_config, full_refresh)
            # DLT_NATIVE: Advanced registry bypass
            elif source_type == SourceType.DLT_NATIVE:
                try:
                    result = self._run_with_timeout(
                        self._run_dlt_native_source,
                        timeout_minutes,
                        source_config,
                        full_refresh
                    )
                except SyncTimeoutError as e:
                    error_message = str(e)
                    console.print(f"[red]❌ {error_message}[/red]")
                    console.print(f"[yellow]ℹ️  Pipeline state has been restored to prevent corruption[/yellow]")

                    # Return failure result
                    duration = (datetime.now() - start_time).total_seconds()
                    history_entry = {
                        "timestamp": start_time.isoformat(),
                        "status": "failed",
                        "duration_seconds": round(duration, 2),
                        "rows_processed": 0,
                        "full_refresh": full_refresh,
                        "error_message": error_message
                    }
                    save_sync_history_entry(self.project_root, source_name, history_entry)

                    log_activity(
                        project_root=self.project_root,
                        level="error",
                        source=source_name,
                        message=f"Sync timeout: {error_message}"
                    )

                    return {
                        "status": "failed",
                        "source": source_name,
                        "error": error_message,
                        "rows_loaded": 0,
                    }
            # All other sources: dlt pipelines with timeout
            else:
                try:
                    result = self._run_with_timeout(
                        self._run_dlt_source,
                        timeout_minutes,
                        source_config,
                        start_date,
                        end_date,
                        full_refresh
                    )
                except SyncTimeoutError as e:
                    error_message = str(e)
                    console.print(f"[red]❌ {error_message}[/red]")
                    console.print(f"[yellow]ℹ️  Pipeline state has been restored to prevent corruption[/yellow]")

                    # Return failure result
                    duration = (datetime.now() - start_time).total_seconds()
                    history_entry = {
                        "timestamp": start_time.isoformat(),
                        "status": "failed",
                        "duration_seconds": round(duration, 2),
                        "rows_processed": 0,
                        "full_refresh": full_refresh,
                        "error_message": error_message
                    }
                    save_sync_history_entry(self.project_root, source_name, history_entry)

                    log_activity(
                        project_root=self.project_root,
                        level="error",
                        source=source_name,
                        message=f"Sync timeout: {error_message}"
                    )

                    return {
                        "status": "failed",
                        "source": source_name,
                        "error": error_message,
                        "rows_loaded": 0,
                    }

            # Calculate duration
            duration = (datetime.now() - start_time).total_seconds()

            # Save sync history and log result
            success = result.get("status") == "success"
            rows_loaded = result.get("rows_loaded", 0)
            error_message = result.get("error")
            uses_replace_mode = result.get("uses_replace_mode", False)

            # Determine if this is a full refresh:
            # Either user explicitly requested it OR source uses replace write_disposition
            is_full_refresh = full_refresh or uses_replace_mode

            history_entry = {
                "timestamp": start_time.isoformat(),
                "status": "success" if success else "failed",
                "duration_seconds": round(duration, 2),
                "rows_processed": rows_loaded,
                "full_refresh": is_full_refresh,
                "error_message": error_message
            }
            save_sync_history_entry(self.project_root, source_name, history_entry)

            if success:
                log_activity(
                    project_root=self.project_root,
                    level="success",
                    source=source_name,
                    message=f"Sync completed in {round(duration, 1)}s - {rows_loaded:,} rows"
                )
            else:
                log_activity(
                    project_root=self.project_root,
                    level="error",
                    source=source_name,
                    message=f"Sync failed: {error_message}"
                )

            return result

        except Exception as e:
            # Get user-friendly error message
            friendly_error = self._analyze_error(e, source_name)
            console.print(f"[red]{friendly_error}[/red]")

            # Log error
            duration = (datetime.now() - start_time).total_seconds()
            error_message = str(e)

            history_entry = {
                "timestamp": start_time.isoformat(),
                "status": "failed",
                "duration_seconds": round(duration, 2),
                "rows_processed": 0,
                "full_refresh": full_refresh,
                "error_message": error_message
            }
            save_sync_history_entry(self.project_root, source_name, history_entry)

            log_activity(
                project_root=self.project_root,
                level="error",
                source=source_name,
                message=f"Sync failed: {error_message}"
            )

            return {
                "status": "failed",
                "source": source_name,
                "error": error_message,
                "rows_loaded": 0,
            }

    def _run_csv_source(
        self, source_config: DataSource, full_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        Run CSV source using custom CSV loader

        Args:
            source_config: Source configuration
            full_refresh: If True, drop existing table and reload

        Returns:
            Load statistics
        """
        if not source_config.csv:
            raise ValueError(f"CSV config missing for source: {source_config.name}")

        # Full refresh: drop existing table and clear metadata
        if full_refresh:
            import duckdb

            conn = duckdb.connect(str(self.duckdb_path))
            try:
                conn.execute(f"DROP TABLE IF EXISTS raw.{source_config.name}")
                console.print("  🔄 Full refresh: dropped existing table")

                # Also clear metadata for this source so files are treated as new
                conn.execute("""
                    DELETE FROM _dango_file_metadata
                    WHERE source_name = ?
                """, [source_config.name])
                console.print("  🔄 Full refresh: cleared file metadata")
            except Exception as e:
                console.print(f"  ⚠️  Could not drop table/metadata: {e}")
            finally:
                conn.close()

        # Run CSV loader
        # Use raw_{source_name} schema pattern (consistent with all other sources)
        target_schema = f"raw_{source_config.name}"
        loader = CSVLoader(self.project_root, self.duckdb_path)
        result = loader.load(
            source_name=source_config.name,
            config=source_config.csv,
            target_schema=target_schema,
        )

        return {
            "status": result.get("status", "success"),
            "source": source_config.name,
            "rows_loaded": result.get("total_rows", 0),
            "files_processed": result.get("new", 0) + result.get("updated", 0),
            **result,
        }

    def _run_dlt_native_source(
        self, source_config: DataSource, full_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        Run dlt native source (registry bypass for advanced users)

        This allows users to:
        1. Use dlt sources not in Dango's registry
        2. Place custom source files in custom_sources/ directory
        3. Configure source directly via sources.yml

        Args:
            source_config: Source configuration with dlt_native config
            full_refresh: Drop pipeline state and reload

        Returns:
            Load statistics
        """
        if not source_config.dlt_native:
            raise ValueError(f"dlt_native config missing for source: {source_config.name}")

        config = source_config.dlt_native
        source_name = source_config.name

        console.print(f"  📦 Loading dlt native source: {config.source_module}.{config.source_function}")
        console.print(f"  [dim]Registry bypass - advanced mode[/dim]")

        # Change to project root so dlt can find .dlt/ directory
        # IMPORTANT: Must happen BEFORE loading source (dlt.secrets.value resolution)
        original_cwd = os.getcwd()
        os.chdir(self.project_root)

        try:
            # Try to import source from custom_sources/ directory first
            import sys
            custom_sources_dir = self.project_root / "custom_sources"

            if custom_sources_dir.exists():
                # Add custom_sources to Python path temporarily
                sys.path.insert(0, str(custom_sources_dir))

            try:
                # Try to import as module (from custom_sources or installed package)
                try:
                    module = importlib.import_module(config.source_module)
                except ImportError:
                    # If not found in custom_sources, try as dlt package
                    try:
                        module = importlib.import_module(f"dlt.sources.{config.source_module}")
                    except ImportError:
                        raise ValueError(
                            f"Could not import source module: {config.source_module}\n"
                            f"  - Not found in custom_sources/ directory\n"
                            f"  - Not found as dlt package (dlt.sources.{config.source_module})\n"
                            f"  - Make sure the module is installed or placed in custom_sources/"
                        )

                # Get source function
                if not hasattr(module, config.source_function):
                    raise ValueError(
                        f"Function '{config.source_function}' not found in module '{config.source_module}'\n"
                        f"  Available functions: {[n for n in dir(module) if not n.startswith('_')]}"
                    )

                source_function = getattr(module, config.source_function)

                # Call source function with provided kwargs
                # dlt resolves dlt.secrets.value parameters at this point
                console.print(f"  [dim]Calling {config.source_function}(**{config.function_kwargs})[/dim]")
                source = source_function(**config.function_kwargs)

            finally:
                # Remove custom_sources from path
                if custom_sources_dir.exists() and str(custom_sources_dir) in sys.path:
                    sys.path.remove(str(custom_sources_dir))

            # Determine dataset name (use custom or default to raw_{source_name})
            dataset_name = config.dataset_name or f"raw_{source_name}"

            # Create pipeline with DuckDB destination
            pipeline_name = config.pipeline_name or source_name
            pipeline = dlt.pipeline(
                pipeline_name=pipeline_name,
                destination=dlt.destinations.duckdb(credentials=str(self.duckdb_path)),
                dataset_name=dataset_name,
            )
        finally:
            # Always restore original working directory
            os.chdir(original_cwd)

        # Full refresh: drop pipeline state
        if full_refresh:
            console.print("  🔄 Full refresh: dropping pipeline state")
            try:
                pipeline.drop()
            except Exception as e:
                console.print(f"  ⚠️  Could not drop pipeline: {e}")

        # Backup dlt state before running
        state_backup = self._backup_dlt_state(pipeline_name)

        try:
            # Run pipeline with retry logic
            load_info = self._run_with_retry(pipeline, source, max_retries=3)

            # Extract load statistics
            stats = self._extract_load_stats(load_info)
            rows_loaded = stats.get("rows_loaded", 0)

            if rows_loaded >= 0:
                # Success
                self._cleanup_state_backup(state_backup)
                console.print(f"  ✓ Loaded {rows_loaded:,} rows")
            else:
                # rows_loaded == -1 means we got a valid LoadInfo but couldn't extract stats
                console.print(f"  ✓ Load completed (unable to count rows)")
                rows_loaded = 0  # Set to 0 for stats

            result = {
                "status": "success",
                "source": source_name,
                "rows_loaded": rows_loaded,
                **stats,
            }
            if getattr(self, '_current_oauth_warning', None):
                result["oauth_warning"] = self._current_oauth_warning
            return result

        except Exception as e:
            # Restore state on failure
            if state_backup:
                console.print("  ⚠️  Restoring pipeline state (failed load)")
                self._restore_dlt_state(source_name, state_backup)
            raise

    def _detect_write_disposition(self, source: Any) -> bool:
        """
        Detect if dlt source uses 'replace' write_disposition.

        This determines if the source performs full refreshes (replace mode)
        or true incremental loading (append/merge mode).

        Args:
            source: dlt source object

        Returns:
            True if source uses replace mode, False otherwise
        """
        try:
            # dlt sources can be callables or DltResource objects
            # Try to extract resources and check their write_disposition
            if hasattr(source, 'resources'):
                # Source has resources attribute (DltSource object)
                resources = source.resources
                if hasattr(resources, 'values'):
                    # resources is a dict-like object
                    for resource in resources.values():
                        if hasattr(resource, 'write_disposition'):
                            if resource.write_disposition == "replace":
                                return True
            elif hasattr(source, '__iter__'):
                # Source is iterable (list of resources)
                for resource in source:
                    if hasattr(resource, 'write_disposition'):
                        if resource.write_disposition == "replace":
                            return True
            elif hasattr(source, 'write_disposition'):
                # Source itself has write_disposition
                if source.write_disposition == "replace":
                    return True

            return False

        except Exception:
            # If we can't detect, assume it's not replace mode (safer default)
            return False

    def _get_dataset_name(
        self,
        source_config: DataSource,
        source_type: Any,
        metadata: Dict[str, Any]
    ) -> str:
        """
        Determine dataset name for a source.

        Always uses raw_{source_name} pattern for schema isolation.
        This follows dlt best practice and industry standard (Airbyte, Fivetran)
        to prevent table name collisions across sources.
        """
        return f"raw_{source_config.name}"

    def _run_dlt_source(
        self,
        source_config: DataSource,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        full_refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        Run dlt pipeline for any verified source (generic implementation)

        This method dynamically imports and executes the correct dlt source
        without hardcoding any source-specific logic.

        Implements state backup/restore to prevent partial failure state corruption.

        Args:
            source_config: Source configuration
            start_date: Override start date
            end_date: Override end date
            full_refresh: Drop pipeline state and reload

        Returns:
            Load statistics
        """
        source_name = source_config.name
        source_type = source_config.type

        # Get source metadata from registry
        metadata = get_source_metadata(source_type.value)
        if not metadata:
            raise ValueError(
                f"Source type '{source_type.value}' not found in registry. "
                f"Available sources: {list(get_source_metadata.keys())}"
            )

        dlt_package = metadata.get("dlt_package")
        dlt_function = metadata.get("dlt_function")
        if not dlt_package or not dlt_function:
            raise ValueError(f"No dlt package/function defined for source type: {source_type.value}")

        console.print(f"  📦 Loading dlt source: {dlt_package}.{dlt_function}")

        # Build source configuration
        source_kwargs = self._build_source_config(
            source_config, source_type, start_date, end_date
        )

        # Merge default_config from registry (e.g., GA4 default queries)
        # Only apply if:
        # 1. Key not already in source_kwargs (from sources.yml)
        # 2. Key not in .dlt/config.toml (user-owned config created at source add time)
        # This ensures user customizations in config.toml are respected
        default_config = metadata.get("default_config", {})
        if default_config:
            # Check what's already in config.toml
            config_toml_keys = set()
            config_toml_path = self.project_root / ".dlt" / "config.toml"
            if config_toml_path.exists():
                try:
                    import tomlkit
                    doc = tomlkit.parse(config_toml_path.read_text())
                    source_section = doc.get("sources", {}).get(source_type_key, {})
                    config_toml_keys = set(source_section.keys())
                except Exception:
                    pass  # If we can't read config.toml, fall back to defaults

            for key, value in default_config.items():
                if key not in source_kwargs and key not in config_toml_keys:
                    source_kwargs[key] = value
                    console.print(f"  [dim]Using default {key} from registry[/dim]")
                elif key in config_toml_keys:
                    console.print(f"  [dim]Using {key} from .dlt/config.toml[/dim]")

        # Apply parameter transforms from registry (e.g., string -> list)
        param_transforms = metadata.get("param_transforms", {})
        for param_name, transform_type in param_transforms.items():
            if param_name in source_kwargs:
                value = source_kwargs[param_name]
                if transform_type == "list" and isinstance(value, str):
                    # Convert single string to list (e.g., sheet name -> [sheet_name])
                    source_kwargs[param_name] = [value]

        # Check OAuth token expiry before attempting sync
        oauth_warning = self._check_oauth_token_expiry(source_type.value, source_name)
        # Store for inclusion in result
        self._current_oauth_warning = oauth_warning

        # Inject OAuth credentials from secrets.toml as explicit kwargs
        # This ensures credentials are passed even if dlt's config resolution misses them
        source_kwargs = self._inject_oauth_credentials(source_type.value, source_kwargs)

        # Set DLT_PROJECT_DIR so dlt finds .dlt/ regardless of when import happened
        # This is dlt's official mechanism for specifying project location
        os.environ["DLT_PROJECT_DIR"] = str(self.project_root)

        # Change to project root as additional fallback
        original_cwd = os.getcwd()
        os.chdir(self.project_root)

        try:
            # Dynamic import of dlt source
            # dlt resolves dlt.secrets.value parameters at this point
            source = self._load_dlt_source(dlt_package, dlt_function, source_kwargs)

            # Detect actual load type from dlt source configuration
            # Check if source uses replace write_disposition (full refresh by design)
            uses_replace_mode = self._detect_write_disposition(source)

            # Determine dataset name based on source characteristics
            # Multi-resource sources → raw_{source_name} (prevents table collisions)
            # Single-resource sources → raw (simple governance)
            dataset_name = self._get_dataset_name(source_config, source_type, metadata)

            # Create pipeline with DuckDB destination
            pipeline = dlt.pipeline(
                pipeline_name=source_name,
                destination=dlt.destinations.duckdb(credentials=str(self.duckdb_path)),
                dataset_name=dataset_name,
            )
        finally:
            # Always restore original working directory
            os.chdir(original_cwd)

        # Full refresh: drop pipeline state
        if full_refresh:
            console.print("  🔄 Full refresh: dropping pipeline state")
            try:
                pipeline.drop()
            except Exception as e:
                console.print(f"  ⚠️  Could not drop pipeline: {e}")

        # Backup dlt state before running (protects against partial failures)
        state_backup = self._backup_dlt_state(source_name)

        try:
            # Run pipeline with retry logic
            load_info = self._run_with_retry(pipeline, source, max_retries=3)

            # Extract load statistics
            stats = self._extract_load_stats(load_info)

            # Success criteria: rows_loaded >= 0 means we got valid data (even if 0 rows)
            # rows_loaded == -1 means we couldn't extract stats but load succeeded
            rows_loaded = stats.get("rows_loaded", 0)

            if rows_loaded >= 0:
                # Success - we got a valid row count (including 0)
                self._cleanup_state_backup(state_backup)
                console.print(f"  ✓ Loaded {rows_loaded:,} rows")

                result = {
                    "status": "success",
                    "source": source_name,
                    "uses_replace_mode": uses_replace_mode,
                    **stats,
                }
                if getattr(self, '_current_oauth_warning', None):
                    result["oauth_warning"] = self._current_oauth_warning
                return result
            else:
                # rows_loaded is -1: unknown row count but load succeeded
                # This should also be treated as success
                self._cleanup_state_backup(state_backup)
                console.print(f"  ✓ Load completed (row count unavailable)")

                result = {
                    "status": "success",
                    "source": source_name,
                    "uses_replace_mode": uses_replace_mode,
                    **stats,
                }
                if getattr(self, '_current_oauth_warning', None):
                    result["oauth_warning"] = self._current_oauth_warning
                return result

        except Exception as e:
            # Pipeline failed - restore previous state
            console.print(f"  ❌ Pipeline failed: {e}")
            console.print(f"  🔄 Restoring previous state...")
            self._restore_dlt_state(state_backup)
            raise

    def _build_source_config(
        self,
        source_config: DataSource,
        source_type: SourceType,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Build source-specific configuration dictionary

        Extracts config from the appropriate field in DataSource based on source type
        and merges with override parameters.

        Args:
            source_config: Source configuration
            source_type: Source type
            start_date: Override start date
            end_date: Override end date

        Returns:
            Dictionary of source-specific parameters
        """
        # Map source type to config field name
        config_field_map = {
            SourceType.REST_API: "rest_api",
            SourceType.FACEBOOK_ADS: "facebook_ads",
            SourceType.GOOGLE_ANALYTICS: "google_analytics",
            SourceType.GOOGLE_SHEETS: "google_sheets",
            SourceType.HUBSPOT: "hubspot",
            SourceType.SALESFORCE: "salesforce",
            SourceType.STRIPE: "stripe",
            SourceType.SHOPIFY: "shopify",
            SourceType.GITHUB: "github",
            SourceType.SLACK: "slack",
            # Add more as we implement them
        }

        # Get the config object
        config_field = config_field_map.get(source_type)
        if not config_field:
            # Use generic_config for sources without dedicated models
            config_obj = source_config.generic_config or {}
        else:
            config_obj = getattr(source_config, config_field, None)
            if config_obj is None:
                # For OAuth sources, config might be in secrets.toml instead of sources.yml
                # This happens when all required params are OAuth-collected (e.g., facebook_ads)
                from dango.ingestion.sources.registry import get_source_metadata, AuthType
                metadata = get_source_metadata(source_type.value)
                if metadata.get("auth_type") == AuthType.OAUTH:
                    # Allow empty config - OAuth injection will provide credentials
                    config_obj = {}
                    console.print(f"  [dim]Note: Using OAuth credentials from secrets.toml[/dim]")
                else:
                    raise ValueError(
                        f"Missing {config_field} configuration for source: {source_config.name}"
                    )

        # Convert Pydantic model to dict if needed
        if hasattr(config_obj, "dict"):
            config_dict = config_obj.dict(exclude_none=True)
        else:
            config_dict = dict(config_obj) if isinstance(config_obj, dict) else {}

        # Dango-specific fields that should NOT be passed to dlt source functions
        DANGO_ONLY_FIELDS = {
            "deduplication",  # Dango's deduplication strategy
            "enabled",        # Dango's source enable/disable flag
            "description",    # Dango's source description
        }

        # Resolve environment variables (fields ending in _env)
        import os

        resolved_config = {}
        for key, value in config_dict.items():
            # Skip Dango-specific fields
            if key in DANGO_ONLY_FIELDS:
                continue

            if key.endswith("_env") and isinstance(value, str):
                # Get actual value from environment
                env_value = os.getenv(value)
                if env_value is None:
                    # Don't add to config - let dlt's auto-injection resolve from secrets.toml
                    # This follows dlt best practice: only pass explicit values, let dlt handle the rest
                    console.print(f"  [dim]Note: {value} not set, using dlt credential resolution[/dim]")
                else:
                    # Remove _env suffix for actual parameter name
                    param_name = key[:-4]  # Remove '_env'
                    resolved_config[param_name] = env_value
            else:
                resolved_config[key] = value

        # Override dates if provided
        if start_date:
            resolved_config["start_date"] = start_date
        if end_date:
            resolved_config["end_date"] = end_date

        return resolved_config

    def _check_oauth_token_expiry(self, source_type: str, source_name: str) -> Optional[Dict[str, Any]]:
        """
        Check if OAuth credentials are expired or expiring soon.

        Raises exception if credentials are expired (prevents sync).
        Returns warning dict if credentials expire within 7 days (allows sync).

        Args:
            source_type: dlt source type (e.g., "facebook_ads")
            source_name: User-defined source name

        Returns:
            Warning dict with expiry info if expiring soon, None otherwise

        Raises:
            ValueError: If credentials are expired
        """
        from dango.oauth.storage import OAuthStorage

        # Check if OAuth credentials exist for this source type
        storage = OAuthStorage(self.project_root)
        oauth_cred = storage.get(source_type)

        # Skip if no OAuth credentials (not an OAuth source)
        if not oauth_cred:
            return None

        # Check expiration status
        if oauth_cred.is_expired():
            # FATAL ERROR: Token expired - prevent sync
            console.print(f"\n[red]❌ OAuth token expired for {source_name}![/red]")
            console.print(f"[yellow]Token expired on:[/yellow] {oauth_cred.expires_at.strftime('%Y-%m-%d')}")
            console.print(f"\n[cyan]To re-authenticate:[/cyan]")
            console.print(f"  1. Run: [bold]dango auth {source_type}[/bold]")
            console.print(f"  2. Follow the OAuth flow to get a new token")
            console.print(f"  3. Run sync again\n")
            raise ValueError(f"OAuth credentials expired for {source_name}. Re-authentication required.")

        elif oauth_cred.is_expiring_soon(days=7):
            # WARNING: Token expires within 7 days - allow sync but warn
            days_left = oauth_cred.days_until_expiry()
            console.print(f"\n[yellow]⚠️  OAuth token for {source_name} expires in {days_left} day(s)[/yellow]")
            console.print(f"[yellow]Expiry date:[/yellow] {oauth_cred.expires_at.strftime('%Y-%m-%d')}")
            console.print(f"[cyan]Re-authenticate soon:[/cyan] dango auth {source_type}\n")
            # Return warning info for end-of-sync summary
            return {
                "source_name": source_name,
                "source_type": source_type,
                "days_left": days_left,
                "expires_at": oauth_cred.expires_at.strftime('%Y-%m-%d'),
            }

        return None

    def _inject_oauth_credentials(self, source_type: str, source_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Inject OAuth credentials from secrets.toml into source kwargs.

        This ensures credentials are explicitly passed to dlt sources even if
        dlt's automatic config resolution fails (e.g., due to import timing).

        Args:
            source_type: dlt source type (e.g., "facebook_ads", "google_sheets")
            source_kwargs: Existing source kwargs

        Returns:
            Updated source kwargs with injected credentials
        """
        import toml

        secrets_file = self.project_root / ".dlt" / "secrets.toml"
        if not secrets_file.exists():
            return source_kwargs

        try:
            secrets = toml.load(secrets_file)
            source_secrets = secrets.get("sources", {}).get(source_type, {})

            if not source_secrets:
                return source_kwargs

            # Google sources use nested credentials object (GcpOAuthCredentials)
            CREDENTIALS_OBJECT_SOURCES = {"google_ads", "google_analytics", "google_sheets"}

            if source_type in CREDENTIALS_OBJECT_SOURCES:
                # Google: inject 'credentials' dict if not already present
                if "credentials" not in source_kwargs and "credentials" in source_secrets:
                    source_kwargs["credentials"] = source_secrets["credentials"]
                    console.print(f"  [dim]Injected OAuth credentials for {source_type}[/dim]")
            else:
                # Non-Google: inject flat parameters (access_token, api_key, account_id, etc.)
                # Common OAuth credential keys to inject
                CREDENTIAL_KEYS = {"access_token", "api_key", "api_secret", "refresh_token", "shop_url", "private_app_password", "account_id"}

                for key in CREDENTIAL_KEYS:
                    # Inject if key is missing OR if key exists but value is None/empty
                    if key in source_secrets:
                        current_value = source_kwargs.get(key)
                        if current_value is None or current_value == "":
                            source_kwargs[key] = source_secrets[key]
                            console.print(f"  [dim]Injected {key} for {source_type}[/dim]")

            return source_kwargs

        except Exception as e:
            console.print(f"  [dim]Warning: Could not inject credentials: {e}[/dim]")
            return source_kwargs

    def _load_dlt_source(self, dlt_package: str, dlt_function: str, source_kwargs: Dict[str, Any]) -> Any:
        """
        Dynamically import and instantiate a dlt source from bundled or built-in sources

        Args:
            dlt_package: Name of the dlt source package (e.g., 'stripe_analytics', 'rest_api')
            dlt_function: Name of the source function to call (e.g., 'stripe_source', 'rest_api_source')
            source_kwargs: Keyword arguments to pass to the source function

        Returns:
            dlt source object
        """
        # Try bundled sources first (verified sources we've included in dango)
        module_path = f"dango.ingestion.dlt_sources.{dlt_package}"

        try:
            console.print(f"    Importing: {module_path}")
            source_module = importlib.import_module(module_path)
        except ImportError:
            # Fall back to built-in dlt sources (e.g., rest_api, filesystem, sql_database)
            module_path = f"dlt.sources.{dlt_package}"
            try:
                console.print(f"    Importing built-in: {module_path}")
                source_module = importlib.import_module(module_path)
            except ImportError as e:
                raise ImportError(
                    f"Could not import dlt source package '{dlt_package}' from bundled sources or built-in dlt sources. "
                    f"Error: {e}"
                )

        try:
            # Get the source function from the module
            console.print(f"    Loading function: {dlt_function}")
            source_function = getattr(source_module, dlt_function)

            # Call source function with config
            console.print(f"    Calling: {dlt_function}(**config)")
            source = source_function(**source_kwargs)

            return source

        except AttributeError as e:
            raise AttributeError(
                f"dlt source package '{dlt_package}' does not have function '{dlt_function}'. "
                f"Available functions: {[name for name in dir(source_module) if not name.startswith('_')]}"
                f"\nError: {e}"
            )
        except Exception as e:
            raise Exception(f"Error loading dlt source '{dlt_package}.{dlt_function}': {e}")

    def _analyze_error(self, error: Exception, source_name: str) -> str:
        """
        Analyze exception and provide user-friendly error message

        Args:
            error: The exception that occurred
            source_name: Name of the data source

        Returns:
            User-friendly error message with guidance
        """
        error_str = str(error).lower()
        error_type = type(error).__name__

        # Authentication errors
        if any(keyword in error_str for keyword in ['unauthorized', '401', 'invalid api key', 'invalid token', 'authentication failed', 'invalid credentials', 'forbidden', '403']):
            return f"""
Authentication Error: Invalid credentials for '{source_name}'

Possible causes:
  • API key/token expired or invalid
  • Insufficient permissions/scopes
  • Wrong environment (test vs live mode)

How to fix:
  1. Check .env file for correct credentials
  2. Verify token hasn't expired
  3. Confirm required permissions are granted
  4. Test credentials manually (curl/API docs)

Example fix:
  # Update .env with correct credentials
  {source_name.upper()}_API_KEY=sk_live_xxx...

Error details: {str(error)}
"""

        # Rate limit errors
        if any(keyword in error_str for keyword in ['rate limit', '429', 'too many requests', 'quota exceeded', 'throttled']):
            return f"""
Rate Limit Error: API rate limit exceeded for '{source_name}'

Possible causes:
  • Syncing too frequently
  • Large dataset hitting API limits
  • Shared API key with other services

How to fix:
  1. Wait a few minutes and try again
  2. Reduce sync frequency
  3. Use smaller date ranges for initial sync
  4. Contact API provider to increase limits

Most sources have these limits:
  • Stripe: 100 req/sec
  • GitHub: 5,000 req/hour
  • Airtable: 5 req/sec

Next steps:
  # Wait and retry
  dango sync --source {source_name}

  # Or use smaller date range
  dango sync --source {source_name} --start-date 2024-12-01 --end-date 2024-12-31

Error details: {str(error)}
"""

        # Schema/data validation errors
        if any(keyword in error_str for keyword in ['schema', 'validation', 'column', 'field', 'type error', 'data type']):
            return f"""
Data Validation Error: Schema mismatch or invalid data for '{source_name}'

Possible causes:
  • API schema changed (new/removed fields)
  • Data type mismatch
  • Staging model out of sync

How to fix:
  1. Run full refresh to reload schema
  2. Check if staging model needs update
  3. Review API documentation for changes

Fix commands:
  # Reload all data with latest schema
  dango sync --source {source_name} --full-refresh

  # Re-run dbt to update models
  dango run

Error details: {str(error)}
"""

        # Connection/network errors
        if any(keyword in error_str for keyword in ['connection', 'timeout', 'network', 'dns', 'unreachable', 'refused']):
            return f"""
Connection Error: Cannot reach API for '{source_name}'

Possible causes:
  • Network connectivity issues
  • API service down
  • Firewall/proxy blocking requests
  • Invalid API endpoint URL

How to fix:
  1. Check internet connection
  2. Verify API status page
  3. Test connectivity: ping/curl
  4. Check firewall/proxy settings

Next steps:
  # Wait and retry
  dango sync --source {source_name}

  # Check API status
  Visit source's status page (docs.api-provider.com/status)

Error details: {str(error)}
"""

        # Generic error with helpful context
        return f"""
Sync Error for '{source_name}':

{str(error)}

Troubleshooting steps:
  1. Check logs: dango start → Activity Logs
  2. Verify configuration: .dango/sources.yml
  3. Test with smaller date range
  4. Try full refresh: dango sync --source {source_name} --full-refresh
  5. Check API documentation for breaking changes

Need help? Visit: https://github.com/anthropics/dango/issues
"""

    def _run_with_retry(
        self, pipeline: dlt.Pipeline, source: Any, max_retries: int = 3
    ) -> LoadInfo:
        """
        Run pipeline with exponential backoff retry logic

        Args:
            pipeline: dlt pipeline object
            source: dlt source object
            max_retries: Maximum number of retry attempts

        Returns:
            LoadInfo from successful run

        Raises:
            Exception: If all retries fail
        """
        last_exception = None

        for attempt in range(1, max_retries + 1):
            try:
                console.print(f"  ⏳ Running pipeline... (attempt {attempt}/{max_retries})")
                load_info = pipeline.run(source)
                return load_info

            except Exception as e:
                last_exception = e
                console.print(f"  ⚠️  Attempt {attempt} failed: {e}")

                if attempt < max_retries:
                    # Exponential backoff: 2^attempt seconds
                    wait_time = 2**attempt
                    console.print(f"  ⏸️  Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    console.print(f"  ❌ All {max_retries} attempts failed")

        raise last_exception

    def _extract_load_stats(self, load_info: LoadInfo) -> Dict[str, Any]:
        """
        Extract statistics from dlt LoadInfo and query database for row counts.

        dlt's LoadInfo doesn't provide row counts directly, so we extract the list
        of loaded tables from metrics and query the database for actual row counts.

        Args:
            load_info: LoadInfo object from pipeline.run()

        Returns:
            Dictionary with load statistics
        """
        stats = {
            "rows_loaded": 0,
            "tables_loaded": [],
            "load_id": load_info.load_id if hasattr(load_info, "load_id") else None,
        }

        # Extract list of tables that were loaded from metrics
        loaded_tables = set()
        try:
            if hasattr(load_info, "metrics") and isinstance(load_info.metrics, dict):
                for load_id, metric_list in load_info.metrics.items():
                    if isinstance(metric_list, list):
                        for metric_entry in metric_list:
                            if isinstance(metric_entry, dict) and 'job_metrics' in metric_entry:
                                for job_metrics in metric_entry['job_metrics'].values():
                                    if hasattr(job_metrics, 'table_name'):
                                        table_name = job_metrics.table_name
                                        # Skip dlt internal tables
                                        if not table_name.startswith('_dlt_'):
                                            loaded_tables.add(table_name)
                                            stats["tables_loaded"].append(table_name)
        except Exception as e:
            console.print(f"[dim]Warning: Could not extract table list from metrics: {e}[/dim]")

        # Query database for row counts
        # Get dataset name (schema) from load_info
        dataset_name = load_info.dataset_name if hasattr(load_info, "dataset_name") else None

        if loaded_tables and dataset_name:
            try:
                import duckdb
                conn = duckdb.connect(str(self.duckdb_path))

                total_rows = 0
                for table_name in loaded_tables:
                    try:
                        result = conn.execute(
                            f'SELECT COUNT(*) FROM "{dataset_name}"."{table_name}"'
                        ).fetchone()
                        if result:
                            table_rows = result[0]
                            total_rows += table_rows
                    except Exception as table_err:
                        console.print(f"[dim]Warning: Could not count rows for {table_name}: {table_err}[/dim]")
                        continue

                conn.close()
                stats["rows_loaded"] = total_rows

            except Exception as e:
                console.print(f"[dim]Warning: Could not query database for row counts: {e}[/dim]")
                # If we can't get row counts, mark as unknown
                stats["rows_loaded"] = -1
        elif not loaded_tables:
            # No tables loaded (or couldn't extract list)
            stats["rows_loaded"] = -1  # -1 means "unknown but successful"

        return stats

    def _backup_dlt_state(self, pipeline_name: str) -> Optional[Path]:
        """
        Backup dlt pipeline state before running.

        dlt stores state in ~/.dlt/pipelines/{pipeline_name}/

        Args:
            pipeline_name: Name of the dlt pipeline

        Returns:
            Path to backup directory if backup was created, None otherwise
        """
        import shutil
        import os

        # Determine dlt state location
        # dlt uses ~/.dlt by default
        dlt_home = Path(os.path.expanduser("~/.dlt"))
        pipeline_state_dir = dlt_home / "pipelines" / pipeline_name

        if not pipeline_state_dir.exists():
            # No state to backup (first run)
            return None

        # Create backup with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = pipeline_state_dir.parent / f"{pipeline_name}_backup_{timestamp}"

        try:
            shutil.copytree(pipeline_state_dir, backup_dir)
            console.print(f"  [dim]💾 State backed up to {backup_dir.name}[/dim]")
            return backup_dir
        except Exception as e:
            console.print(f"  [dim]⚠️  Could not backup state: {e}[/dim]")
            return None

    def _restore_dlt_state(self, backup_dir: Optional[Path]):
        """
        Restore dlt pipeline state from backup.

        Args:
            backup_dir: Path to backup directory (from _backup_dlt_state)
        """
        import shutil

        if not backup_dir or not backup_dir.exists():
            console.print(f"  [dim]ℹ️  No state backup to restore[/dim]")
            return

        # Extract pipeline name from backup directory name
        # Format: {pipeline_name}_backup_{timestamp}
        backup_name = backup_dir.name
        pipeline_name = "_".join(backup_name.split("_backup_")[0].split("_"))

        # Determine original state location
        pipeline_state_dir = backup_dir.parent / pipeline_name

        try:
            # Remove current (corrupted) state
            if pipeline_state_dir.exists():
                shutil.rmtree(pipeline_state_dir)

            # Restore from backup
            shutil.copytree(backup_dir, pipeline_state_dir)
            console.print(f"  [green]✓ State restored from backup[/green]")

            # Clean up backup
            shutil.rmtree(backup_dir)
            console.print(f"  [dim]✓ Backup cleaned up[/dim]")

        except Exception as e:
            console.print(f"  [yellow]⚠️  Could not restore state: {e}[/yellow]")
            console.print(f"  [dim]Backup preserved at: {backup_dir}[/dim]")

    def _cleanup_state_backup(self, backup_dir: Optional[Path]):
        """
        Clean up state backup after successful pipeline run.

        Args:
            backup_dir: Path to backup directory (from _backup_dlt_state)
        """
        import shutil

        if not backup_dir or not backup_dir.exists():
            return

        try:
            shutil.rmtree(backup_dir)
            console.print(f"  [dim]✓ State backup cleaned up[/dim]")
        except Exception as e:
            console.print(f"  [dim]⚠️  Could not clean up backup: {e}[/dim]")


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def _display_dbt_output(dbt_output: str) -> None:
    """
    Parse and display dbt model execution details.

    Extracts per-model status and timing from dbt output to show consistent
    logging across all data source types.

    Args:
        dbt_output: Raw dbt stdout+stderr output
    """
    import re

    # Strip ANSI color codes from dbt output
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean_output = ansi_escape.sub('', dbt_output)

    # Parse dbt output for model execution lines
    # Format: "1 of 3 OK created sql table model staging.stg_name ... [OK in 0.10s]"
    # or: "1 of 3 ERROR creating sql table model ... [ERROR in 0.10s]"

    # Match lines like: "1 of 3 OK created sql table model staging.stg_stripe_test_1__charge ............ [OK in 0.10s]"
    success_pattern = r'(\d+) of (\d+) (OK|ERROR|SKIP) .*? model (\w+)\.(\S+).*?\[(OK|ERROR|SKIP)(?: in ([\d.]+)s)?\]'

    found_any = False
    for line in clean_output.split('\n'):
        match = re.search(success_pattern, line)
        if match:
            found_any = True
            seq_num, total, status_word, schema, model_name, result, timing = match.groups()

            # Format output consistently
            if result == "OK":
                status_icon = "✓"
                status_color = "green"
            elif result == "ERROR":
                status_icon = "✗"
                status_color = "red"
            elif result == "SKIP":
                status_icon = "⊘"
                status_color = "yellow"
            else:
                continue

            timing_str = f" in {timing}s" if timing else ""
            console.print(f"  [{status_color}]{status_icon}[/{status_color}] {schema}.{model_name}{timing_str}")


def run_sync(
    project_root: Path,
    sources: List[DataSource],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    full_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Sync multiple sources and return summary

    Args:
        project_root: Path to project root
        sources: List of source configurations
        start_date: Override start date
        end_date: Override end date
        full_refresh: Full refresh mode

    Returns:
        Summary dictionary with success/failed counts
    """
    runner = DltPipelineRunner(project_root)

    results = []
    success_sources = []
    failed_sources = []
    skipped_sources = []

    for source_config in sources:
        if not source_config.enabled:
            console.print(f"\n⏭️  Skipping disabled source: {source_config.name}")
            skipped_sources.append(source_config.name)
            continue

        result = runner.run_source(source_config, start_date, end_date, full_refresh)
        results.append(result)

        if result.get("status") == "success":
            success_sources.append(source_config.name)
        else:
            failed_sources.append({
                "name": source_config.name,
                "error": result.get("error", "Unknown error")
            })

    # Print detailed summary
    console.print(f"\n{'='*60}")
    console.print(f"📊 Sync Summary:")
    console.print(f"{'='*60}\n")

    if success_sources:
        console.print(f"[green]✓ Succeeded ({len(success_sources)}):[/green]")
        for name in success_sources:
            console.print(f"  • {name}")
        console.print()

    if failed_sources:
        console.print(f"[red]✗ Failed ({len(failed_sources)}):[/red]")
        for item in failed_sources:
            console.print(f"  • {item['name']}")
            console.print(f"    [dim]{item['error']}[/dim]")
        console.print()

    if skipped_sources:
        console.print(f"[dim]⏭  Skipped ({len(skipped_sources)}):[/dim]")
        for name in skipped_sources:
            console.print(f"  • {name} [dim](disabled)[/dim]")
        console.print()

    # Overall stats
    total = len(success_sources) + len(failed_sources)
    if total > 0:
        success_rate = (len(success_sources) / total) * 100
        console.print(f"Overall: {len(success_sources)}/{total} sources succeeded ({success_rate:.0f}%)")

    # Collect OAuth warnings (will be displayed at very end of sync in main.py)
    oauth_warnings = [r.get("oauth_warning") for r in results if r.get("oauth_warning")]

    console.print(f"{'='*60}\n")

    # Auto-generate staging models for successful sources
    if success_sources:
        console.print("🔄 [bold]Generating staging models...[/bold]\n")

        from dango.transformation.generator import DbtModelGenerator

        generator = DbtModelGenerator(project_root)

        # Get successful source configs
        successful_configs = [src for src in sources if src.name in success_sources]

        gen_summary = generator.generate_all_models(
            sources=successful_configs,
            generate_schema_yml=True,  # Required for dbt source() references
            skip_customized=True,  # Don't overwrite user customizations
        )

        if gen_summary.get("generated"):
            console.print(f"[green]✓ Generated/updated {len(gen_summary['generated'])} staging model(s)[/green]")
            for item in gen_summary['generated']:
                console.print(f"  • {item['source']}")

        if gen_summary.get("skipped"):
            # Categorize skipped models by reason type
            customized = [s for s in gen_summary['skipped'] if 'customized' in s.get('reason', '').lower()]
            not_found = [s for s in gen_summary['skipped'] if 'not found' in s.get('reason', '').lower()]

            if customized:
                console.print(f"[dim]⏭  Skipped {len(customized)} model(s) (user-customized)[/dim]")
                for item in customized:
                    console.print(f"  • {item['source']}: {item.get('endpoint', '')}")

            if not_found:
                console.print(f"[dim]⏭  Skipped {len(not_found)} model(s) (tables pending)[/dim]")
                for item in not_found:
                    console.print(f"  • {item['source']}: {item['reason']}")

        console.print()

        # Run dbt to create staging/marts tables
        # Use selective runs to only process models dependent on synced sources
        console.print("🔄 [bold]Running dbt models...[/bold]\n")
        from dango.transformation import run_dbt_models

        # Build source-based selection criteria
        # Format: "source:source1+ source:source2+" (runs models dependent on these sources)
        if success_sources:
            select_criteria = " ".join([f"source:{source}+" for source in success_sources])
            console.print(f"[dim]Targeting models for sources: {', '.join(success_sources)}[/dim]")
            dbt_success, dbt_output = run_dbt_models(project_root, select=select_criteria)
        else:
            # No sources synced, run all models (backward compatibility)
            dbt_success, dbt_output = run_dbt_models(project_root)

        if dbt_success:
            # Parse and display dbt model execution details
            _display_dbt_output(dbt_output)
            console.print("[green]✓ dbt models executed successfully[/green]")

            # Generate dbt docs
            console.print("[dim]Generating dbt documentation...[/dim]")
            from dango.transformation import generate_dbt_docs

            docs_success, docs_output = generate_dbt_docs(project_root)
            if docs_success:
                console.print("[green]✓ dbt docs generated[/green]")
            else:
                console.print("[yellow]⚠️  dbt docs generation failed (non-critical)[/yellow]")

            # Refresh Metabase connection to see new data
            console.print("[dim]Refreshing Metabase connection...[/dim]")
            from dango.visualization.metabase import refresh_metabase_connection, sync_metabase_schema

            if refresh_metabase_connection(project_root):
                console.print("[green]✓ Metabase connection refreshed[/green]")

                # Sync schema metadata to ensure all tables are discovered and descriptions updated
                console.print("[dim]Syncing Metabase schema metadata...[/dim]")
                if sync_metabase_schema(project_root):
                    console.print("[green]✓ Metabase schema synced[/green]")
            else:
                console.print("[dim]ℹ Metabase not running (will sync when started)[/dim]")
        else:
            console.print("[red]✗ dbt run failed[/red]")
            console.print(f"[dim]{dbt_output}[/dim]")
            console.print("[yellow]⚠️  Staging/marts tables were not created[/yellow]")

        console.print()

    return {
        "success_count": len(success_sources),
        "failed_count": len(failed_sources),
        "skipped_count": len(skipped_sources),
        "success_sources": success_sources,
        "failed_sources": failed_sources,
        "skipped_sources": skipped_sources,
        "results": results,
        "oauth_warnings": oauth_warnings,  # For display at very end of sync
    }
