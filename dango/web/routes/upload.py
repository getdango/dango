"""dango/web/routes/upload.py

File upload, listing, and deletion endpoints for CSV and local_files sources.
"""

import glob
import logging
import os
import time
import traceback
from datetime import datetime
from pathlib import Path

import duckdb
import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile

from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.validation import sanitize_path_component, validate_source_name
from dango.web.helpers import (
    append_log_entry,
    get_duckdb_path,
    get_project_root,
    get_source_row_count,
    load_sources_config,
    save_sync_history_entry,
)
from dango.web.routes.sync import run_sync_task
from dango.web.routes.websocket import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["upload"])


@router.post("/api/sources/{source_name}/upload-csv")
async def upload_csv_to_source(
    source_name: str,
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None,
    trigger_sync: bool = False,  # Default to false - let frontend control when to sync
    user: User = Depends(require_permission("csv.upload")),
):
    """Upload a CSV file to an existing pre-configured CSV source.

    By default, this only saves the file to disk without triggering sync.
    This allows batch uploads to complete quickly, then trigger ONE sync at the end.

    Args:
        source_name: Name of the existing CSV source (from path)
        file: CSV file to upload
        trigger_sync: If True, immediately trigger sync after upload (default: False)

    Returns:
        Success message and file info
    """
    source_name = validate_source_name(source_name)
    try:
        import aiofiles

        project_root = get_project_root()

        # Load sources configuration
        sources_file = project_root / ".dango" / "sources.yml"
        if not sources_file.exists():
            raise HTTPException(status_code=404, detail="No sources configured")

        with open(sources_file, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        # Find the source
        source_config = None
        for source in config.get("sources", []):
            if source.get("name") == source_name:
                source_config = source
                break

        if not source_config:
            raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")

        # Validate source is a file-based type
        source_type = source_config.get("type")
        if source_type not in ("csv", "local_files"):
            raise HTTPException(
                status_code=400,
                detail=f"Source '{source_name}' is not a file source (type: {source_type})",
            )

        # Validate file type
        allowed_extensions = {".csv", ".json", ".jsonl", ".ndjson", ".parquet"}
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename provided")
        file_ext = Path(file.filename).suffix.lower()
        if source_type == "csv" and file_ext != ".csv":
            raise HTTPException(
                status_code=400, detail="Only CSV files are supported for this source"
            )
        if source_type == "local_files" and file_ext not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file format. Supported: {', '.join(sorted(allowed_extensions))}",
            )

        # Get directory from source config
        config_key = "local_files" if source_type == "local_files" else "csv"
        file_config = source_config.get(config_key, {})
        directory = file_config.get("directory", "data")

        # Resolve directory path (could be relative or absolute)
        data_dir = Path(directory)
        if not data_dir.is_absolute():
            data_dir = project_root / directory

        # Create data directory if it doesn't exist
        data_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize the uploaded filename to prevent directory traversal
        safe_filename = sanitize_path_component(file.filename or "unnamed")

        # Check if file already exists
        file_path = data_dir / safe_filename
        # Defense-in-depth: ensure resolved path stays within data_dir
        if not file_path.resolve().is_relative_to(data_dir.resolve()):
            raise HTTPException(status_code=400, detail="Invalid filename.")
        if file_path.exists():
            raise HTTPException(
                status_code=409,
                detail=f"File '{safe_filename}' already exists. Delete the existing file first or rename your file.",
            )
        async with aiofiles.open(file_path, "wb") as buffer:
            content = await file.read()
            await buffer.write(content)

        logger.info(f"Uploaded file for source '{source_name}': {file_path}")

        # Broadcast upload event via WebSocket
        await ws_manager.broadcast(
            {
                "event": "file_uploaded",
                "source": source_name,
                "message": f"File {safe_filename} uploaded to {source_name}",
                "timestamp": datetime.now().isoformat(),
            }
        )

        # Only trigger sync if explicitly requested (for batch upload optimization)
        if trigger_sync and background_tasks:
            background_tasks.add_task(
                run_sync_task, source_name, full_refresh=False, start_date=None, end_date=None
            )
            logger.info(f"Triggered immediate sync for source '{source_name}'")

        return {
            "success": True,
            "message": f"File uploaded successfully: {safe_filename}"
            + (" - Sync started." if trigger_sync else ""),
            "source_name": source_name,
            "file_name": safe_filename,
            "auto_sync": trigger_sync,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading CSV: {e}")
        raise HTTPException(status_code=500, detail="Upload failed") from e


@router.get("/api/sources/{source_name}/csv-files")
async def get_csv_files(source_name: str):
    """Get CSV files for a source - both on disk and loaded into database.

    Args:
        source_name: Name of the CSV source

    Returns:
        List of files with their status (on_disk, loaded, both)
    """
    source_name = validate_source_name(source_name)
    try:
        project_root = get_project_root()

        # Load sources configuration
        sources_file = project_root / ".dango" / "sources.yml"
        if not sources_file.exists():
            raise HTTPException(status_code=404, detail="No sources configured")

        with open(sources_file, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        # Find the source
        source_config = None
        for source in config.get("sources", []):
            if source.get("name") == source_name:
                source_config = source
                break

        if not source_config:
            raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")

        # Validate source is a file-based type
        source_type = source_config.get("type")
        if source_type not in ("csv", "local_files"):
            raise HTTPException(
                status_code=400, detail=f"Source '{source_name}' is not a file source"
            )

        config_key = "local_files" if source_type == "local_files" else "csv"
        file_config = source_config.get(config_key, {})
        directory = file_config.get("directory", "data")
        file_pattern = file_config.get(
            "file_pattern", "*" if source_type == "local_files" else "*.csv"
        )

        # Resolve directory path
        if not Path(directory).is_absolute():
            directory = project_root / directory
        else:
            directory = Path(directory)

        # Get files on disk
        files_on_disk = {}
        if directory.exists():
            pattern = str(directory / file_pattern)
            for filepath in glob.glob(pattern):
                filename = os.path.basename(filepath)
                stat = os.stat(filepath)
                files_on_disk[filepath] = {
                    "filename": filename,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "path": filepath,
                }

        # Get files from metadata table
        files_loaded = {}
        duckdb_path = get_duckdb_path()
        if duckdb_path.exists():
            conn = duckdb.connect(str(duckdb_path), config={"access_mode": "read_only"})

            # Check if metadata table exists
            result = conn.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_name = '_dango_file_metadata'
            """).fetchall()

            if result:
                # Get loaded files for this source
                # Include deleted files only if loaded within last 7 days
                rows = conn.execute(
                    """
                    SELECT file_path, rows_loaded, loaded_at, status
                    FROM _dango_file_metadata
                    WHERE source_name = ?
                    AND (status != 'deleted' OR loaded_at > NOW() - INTERVAL 7 DAY)
                    ORDER BY loaded_at DESC
                """,
                    [source_name],
                ).fetchall()

                for row in rows:
                    filepath, rows_loaded, loaded_at, status = row
                    filename = os.path.basename(filepath)
                    files_loaded[filepath] = {
                        "filename": filename,
                        "rows_loaded": rows_loaded,
                        "loaded_at": loaded_at.isoformat() if loaded_at else None,
                        "status": status,
                        "path": filepath,
                    }

            conn.close()

        # Combine information
        all_files = []

        # Files on disk
        for filepath, info in files_on_disk.items():
            file_info = {
                "filename": info["filename"],
                "path": filepath,
                "size": info["size"],
                "modified": info["modified"],
                "on_disk": True,
                "loaded": filepath in files_loaded,
            }

            if filepath in files_loaded:
                file_info["rows_loaded"] = files_loaded[filepath]["rows_loaded"]
                file_info["loaded_at"] = files_loaded[filepath]["loaded_at"]
                file_info["status"] = files_loaded[filepath]["status"]

            all_files.append(file_info)

        # Files in database but not on disk (deleted)
        for filepath, info in files_loaded.items():
            if filepath not in files_on_disk:
                all_files.append(
                    {
                        "filename": info["filename"],
                        "path": filepath,
                        "size": None,
                        "modified": None,
                        "on_disk": False,
                        "loaded": True,
                        "rows_loaded": info["rows_loaded"],
                        "loaded_at": info["loaded_at"],
                        "status": "file_deleted",
                    }
                )

        return {
            "source_name": source_name,
            "directory": str(directory),
            "file_pattern": file_pattern,
            "files": all_files,
            "total_files": len(all_files),
            "files_on_disk": len(files_on_disk),
            "files_loaded": len(files_loaded),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting CSV files: {e}")
        raise HTTPException(status_code=500, detail="Failed to get CSV files") from e


@router.delete("/api/sources/{source_name}/csv-files")
async def delete_csv_file(
    source_name: str,
    file_path: str = Query(..., description="Full path to file to delete"),
    background_tasks: BackgroundTasks = None,
    user: User = Depends(require_permission("csv.delete")),
):
    """Delete a CSV file from filesystem and trigger sync to update database.

    VERSION: 2025-11-04-v2 (immediate DB cleanup, delete metadata record)

    Args:
        source_name: Name of the CSV source
        file_path: Full path to the file to delete
        background_tasks: FastAPI background tasks

    Returns:
        Success message with deletion details
    """
    source_name = validate_source_name(source_name)
    logger.info(
        f"DELETE ENDPOINT CALLED - VERSION 2025-11-04-v2 - source: {source_name}, file: {file_path}"
    )

    try:
        project_root = get_project_root()
        sources_config = load_sources_config()

        # Find source config
        source_config = next((s for s in sources_config if s.get("name") == source_name), None)
        if not source_config:
            raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")

        # Verify source is a file-based type
        source_type = source_config.get("type")
        if source_type not in ("csv", "local_files"):
            raise HTTPException(
                status_code=400, detail=f"Source '{source_name}' is not a file source"
            )

        # Verify file exists
        file_to_delete = Path(file_path)
        if not file_to_delete.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

        # Verify file is in the source directory (security check)
        config_key = "local_files" if source_type == "local_files" else "csv"
        source_directory = source_config.get(config_key, {}).get("directory")
        if not source_directory:
            raise HTTPException(status_code=500, detail="Source directory not configured")

        # Resolve source directory relative to project root
        source_dir = project_root / source_directory
        source_dir = source_dir.resolve()

        try:
            file_to_delete.resolve().relative_to(source_dir)
        except ValueError:
            raise HTTPException(
                status_code=403, detail="Cannot delete files outside source directory"
            ) from None

        # Delete data from database FIRST (before deleting file)
        # This ensures data is cleaned up immediately and user sees instant feedback
        filename = file_to_delete.name
        logger.info(f"Starting DB cleanup for file: {filename}")

        # Connect to DuckDB (reuse connection for both data and metadata cleanup)
        from dango.config import ConfigLoader

        loader = ConfigLoader(project_root)
        config = loader.load_config()
        duckdb_path = project_root / config.platform.duckdb_path
        duckdb_path_resolved = duckdb_path.resolve()
        logger.info(f"DuckDB path (config): {config.platform.duckdb_path}")
        logger.info(f"DuckDB path (resolved): {duckdb_path_resolved}")
        logger.info(f"File exists: {duckdb_path_resolved.exists()}")
        logger.info(
            f"File size: {duckdb_path_resolved.stat().st_size if duckdb_path_resolved.exists() else 'N/A'}"
        )

        conn = None
        try:
            conn = duckdb.connect(str(duckdb_path_resolved))

            # Debug: List all tables to verify connection
            all_tables = conn.execute("""
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema NOT LIKE 'pg_%'
                ORDER BY table_schema, table_name
            """).fetchall()
            logger.info(f"Tables in database: {all_tables}")

            # Delete rows for this file from raw table (if table exists)
            target_table = f'"raw"."{source_name}"'
            logger.info(f"Deleting from {target_table} WHERE _dango_filename = '{filename}'")

            # Check if table exists first
            table_exists = (
                conn.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = 'raw' AND table_name = ?
                    """,
                    [source_name],
                ).fetchone()[0]
                > 0
            )

            if table_exists:
                conn.execute(f"DELETE FROM {target_table} WHERE _dango_filename = ?", [filename])
                rows_deleted = conn.execute("SELECT changes()").fetchone()[0]
                logger.info(f"Deleted {rows_deleted} rows from {target_table}")
            else:
                logger.warning(
                    f"Table {target_table} doesn't exist - file was never synced. Skipping data deletion."
                )
                rows_deleted = 0

        except Exception as e:
            logger.error(f"ERROR during DB data deletion: {type(e).__name__}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            # Continue with metadata cleanup even if data deletion fails

        # ALWAYS delete metadata record (even if data deletion failed)
        # Reuse the same connection
        try:
            if conn is None:
                logger.error("Connection is None - cannot clean up metadata")
            else:
                logger.info(
                    f"Deleting metadata record for source={source_name}, file_path={file_path}"
                )
                conn.execute(
                    """
                    DELETE FROM _dango_file_metadata
                    WHERE source_name = ? AND file_path = ?
                    """,
                    [source_name, file_path],
                )
                metadata_deleted = conn.execute("SELECT changes()").fetchone()[0]
                logger.info(f"Deleted {metadata_deleted} metadata records")
                logger.info(f"Metadata cleanup complete for file: {filename}")

        except Exception as e:
            logger.error(f"ERROR during metadata cleanup: {type(e).__name__}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")

        finally:
            # Always close connection
            if conn is not None:
                try:
                    conn.close()
                    logger.info("DB connection closed")
                except Exception:
                    pass

        # Delete file from filesystem
        os.remove(file_path)
        logger.info(f"Deleted file: {file_path}")

        # Broadcast deletion event
        await ws_manager.broadcast(
            {
                "event": "csv_deleted",
                "source": source_name,
                "message": f"Deleted {filename}",
                "timestamp": datetime.now().isoformat(),
            }
        )

        # Log activity
        append_log_entry(
            {
                "timestamp": datetime.now().isoformat(),
                "level": "info",
                "source": source_name,
                "message": f"Deleted file: {filename}",
            }
        )

        # Trigger dbt run for downstream models (in background)
        if background_tasks:
            background_tasks.add_task(run_dbt_after_delete, source_name)
            logger.info(f"Triggered dbt run for {source_name} after file deletion")

        return {
            "success": True,
            "message": f"File deleted: {filename}",
            "file_path": file_path,
            "source_name": source_name,
            "background_sync": True,
        }

    except HTTPException as he:
        logger.error(f"HTTP Exception during delete: {he.status_code} - {he.detail}")
        raise
    except Exception as e:
        logger.error(f"UNEXPECTED ERROR deleting CSV file: {type(e).__name__}: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Failed to delete file") from e


async def run_dbt_after_delete(source_name: str):
    """Run dbt models after file deletion to update staging/downstream tables.

    This ensures that when files are deleted, the staging models reflect
    the current state of the raw data (with rows removed)
    """
    from dango.utils import DbtLock, DbtLockError

    start_time = time.time()
    sync_timestamp = datetime.now().isoformat()
    project_root = get_project_root()

    # Try to acquire lock before running dbt
    lock = None
    try:
        lock = DbtLock(
            project_root=project_root,
            source="ui",
            operation=f"dbt run after {source_name} file deletion",
        )
        lock.acquire()
    except DbtLockError as e:
        # Lock is held by another process - broadcast error and return
        error_msg = str(e).split("\n")[0]
        await ws_manager.broadcast(
            {
                "event": "dbt_run_all_failed",
                "source": f"dbt (triggered by {source_name} delete)",
                "message": error_msg,
                "timestamp": datetime.now().isoformat(),
            }
        )
        append_log_entry(
            {
                "timestamp": datetime.now().isoformat(),
                "level": "error",
                "source": f"dbt (triggered by {source_name} delete)",
                "message": f"dbt run blocked: {error_msg}",
            }
        )
        logger.warning(f"Could not acquire dbt lock for delete operation: {e}")
        return

    try:
        from dango.transformation import run_dbt_models

        # Log dbt start
        append_log_entry(
            {
                "timestamp": sync_timestamp,
                "level": "info",
                "source": f"dbt (triggered by {source_name} delete)",
                "message": f"Running dbt models for {source_name} after file deletion",
            }
        )

        # Broadcast dbt started
        await ws_manager.broadcast(
            {
                "event": "dbt_run_all_started",
                "source": f"dbt (triggered by {source_name} delete)",
                "message": "Updating models after file deletion",
                "timestamp": sync_timestamp,
            }
        )

        # Run dbt for this source and downstream models
        # Use source:source_name+ to run all models dependent on this source
        select_criteria = f"source:{source_name}+"
        dbt_success, dbt_output = run_dbt_models(
            get_project_root(), select=select_criteria, full_refresh=True
        )

        # Calculate duration
        duration = time.time() - start_time

        # Get current row count
        rows_processed = get_source_row_count(source_name) or 0

        if dbt_success:
            # Log success
            append_log_entry(
                {
                    "timestamp": datetime.now().isoformat(),
                    "level": "success",
                    "source": f"dbt (triggered by {source_name} delete)",
                    "message": "dbt models updated successfully",
                }
            )

            # Broadcast completion
            await ws_manager.broadcast(
                {
                    "event": "dbt_run_all_completed",
                    "source": f"dbt (triggered by {source_name} delete)",
                    "message": "Models updated after file deletion",
                    "timestamp": datetime.now().isoformat(),
                }
            )

            # Save sync history with success
            history_entry = {
                "timestamp": sync_timestamp,
                "status": "success",
                "duration_seconds": round(duration, 2),
                "rows_processed": rows_processed,
                "full_refresh": False,
                "error_message": None,
            }
            save_sync_history_entry(source_name, history_entry)
        else:
            # Log failure - dbt_output already contains "dbt run failed:" prefix
            append_log_entry(
                {
                    "timestamp": datetime.now().isoformat(),
                    "level": "error",
                    "source": f"dbt (triggered by {source_name} delete)",
                    "message": dbt_output,  # Don't add extra "dbt run failed:" prefix
                }
            )

            # Broadcast failure
            await ws_manager.broadcast(
                {
                    "event": "dbt_run_all_failed",
                    "source": f"dbt (triggered by {source_name} delete)",
                    "message": "dbt run failed",
                    "timestamp": datetime.now().isoformat(),
                }
            )

            # Save sync history with failure
            history_entry = {
                "timestamp": sync_timestamp,
                "status": "failed",
                "duration_seconds": round(duration, 2),
                "rows_processed": 0,
                "full_refresh": False,
                "error_message": dbt_output,
            }
            save_sync_history_entry(source_name, history_entry)

    except Exception as e:
        logger.error(f"Error running dbt after delete: {e}")
        duration = time.time() - start_time

        append_log_entry(
            {
                "timestamp": datetime.now().isoformat(),
                "level": "error",
                "source": f"dbt (triggered by {source_name} delete)",
                "message": f"Error running dbt: {str(e)}",
            }
        )

        # Save sync history with exception
        history_entry = {
            "timestamp": sync_timestamp,
            "status": "failed",
            "duration_seconds": round(duration, 2),
            "rows_processed": 0,
            "full_refresh": False,
            "error_message": str(e),
        }
        save_sync_history_entry(source_name, history_entry)
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
