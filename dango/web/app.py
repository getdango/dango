"""
Dango Web UI Backend

FastAPI application for monitoring and managing Dango data pipelines.
Provides REST API endpoints and WebSocket support for real-time updates.
"""

from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
import asyncio
import json
import logging
import os

import dango

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks, UploadFile, File, Form, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response, RedirectResponse
from pydantic import BaseModel
import yaml
import duckdb
import shutil


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Pydantic models for API responses
class TableInfo(BaseModel):
    """Table information for multi-resource sources"""
    name: str
    row_count: int
    schema: str


class SourceStatus(BaseModel):
    """Source status information"""
    name: str
    type: str
    enabled: bool
    last_sync: Optional[str] = None
    row_count: Optional[int] = None
    status: str = "unknown"  # "synced", "syncing", "failed", "unknown"
    freshness: Optional[Dict[str, Any]] = None  # Data freshness information
    tables: Optional[List[TableInfo]] = None  # Per-table breakdown for multi-resource sources


class ServiceHealth(BaseModel):
    """Service health check response"""
    status: str
    dango_version: str = "0.1.0"
    services: Dict[str, str]
    uptime: str


class SyncRequest(BaseModel):
    """Sync request parameters"""
    full_refresh: bool = False
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class SyncResponse(BaseModel):
    """Sync response"""
    success: bool
    message: str
    source_name: str
    started_at: str


class LogEntry(BaseModel):
    """Log entry"""
    timestamp: str
    level: str
    message: str


class WatcherStatus(BaseModel):
    """File watcher status information"""
    running: bool
    pid: Optional[int] = None
    auto_sync_enabled: bool
    auto_dbt_enabled: bool
    debounce_seconds: int
    watch_patterns: List[str]
    watch_directories: List[str]
    log_file: Optional[str] = None


# WebSocket connection manager
class ConnectionManager:
    """Manages WebSocket connections for real-time updates"""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        """Accept new WebSocket connection"""
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        """Remove WebSocket connection"""
        self.active_connections.remove(websocket)
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients and persist to logs"""
        # Persist to logs
        log_entry = {
            "timestamp": message.get("timestamp", datetime.now().isoformat()),
            "level": self._get_log_level(message.get("event", "")),
            "source": message.get("source", "system"),
            "message": message.get("message", str(message.get("event", ""))),
        }
        append_log_entry(log_entry)

        # Broadcast to WebSocket clients
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error sending to WebSocket: {e}")
                disconnected.append(connection)

        # Clean up disconnected clients
        for connection in disconnected:
            try:
                self.active_connections.remove(connection)
            except ValueError:
                pass

    def _get_log_level(self, event: str) -> str:
        """Determine log level from event type"""
        if "completed" in event or "success" in event:
            return "success"
        elif "failed" in event or "error" in event:
            return "error"
        elif "warning" in event:
            return "warning"
        else:
            return "info"


# Global WebSocket manager
ws_manager = ConnectionManager()


# FastAPI app initialization
def create_app(project_root: Optional[Path] = None) -> FastAPI:
    """
    Create and configure FastAPI application

    Args:
        project_root: Path to Dango project root (defaults to current directory)

    Returns:
        Configured FastAPI app
    """
    app = FastAPI(
        title="Dango API",
        description="API for managing and monitoring Dango data pipelines",
        version="0.1.0",
        docs_url=None,  # Disable default docs, we'll create custom ones with navbar
        redoc_url=None  # Disable default redoc
    )

    # Add CORS middleware for development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # In production, restrict to specific origins
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store project root in app state
    if project_root is None:
        project_root = Path.cwd()
    app.state.project_root = project_root

    return app


app = create_app()

# Mount static files directory
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# Helper functions
def get_project_root() -> Path:
    """Get project root from app state"""
    return app.state.project_root


def load_sources_config() -> List[Dict[str, Any]]:
    """Load sources configuration from .dango/sources.yml"""
    sources_file = get_project_root() / ".dango" / "sources.yml"

    if not sources_file.exists():
        return []

    try:
        with open(sources_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
            return config.get('sources', [])
    except Exception as e:
        logger.error(f"Error loading sources config: {e}")
        return []


def get_duckdb_path() -> Path:
    """Get path to DuckDB database"""
    return get_project_root() / "data" / "warehouse.duckdb"


def get_dbt_manifest() -> Optional[Dict[str, Any]]:
    """Load dbt manifest.json"""
    manifest_path = get_project_root() / "dbt" / "target" / "manifest.json"

    if not manifest_path.exists():
        return None

    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading dbt manifest: {e}")
        return None


def get_dbt_model_row_count(schema: str, model_name: str) -> Optional[int]:
    """Get row count for a dbt model from DuckDB"""
    db_path = get_duckdb_path()

    if not db_path.exists():
        return None

    try:
        conn = duckdb.connect(str(db_path), read_only=True)

        # Check if table exists
        result = conn.execute(f"""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema = '{schema}' AND table_name = '{model_name}'
        """).fetchone()

        if result and result[0] > 0:
            # Table exists, get row count
            count_result = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{model_name}"').fetchone()
            conn.close()
            return count_result[0] if count_result else 0

        conn.close()
        return None

    except Exception as e:
        logger.error(f"Error getting row count for {schema}.{model_name}: {e}")
        return None


def get_dbt_model_last_run() -> Optional[str]:
    """Get last dbt run timestamp from run_results.json"""
    project_root = get_project_root()
    run_results_path = project_root / "dbt" / "target" / "run_results.json"

    if not run_results_path.exists():
        return None

    try:
        with open(run_results_path, 'r', encoding='utf-8') as f:
            run_results = json.load(f)

        # Get the generated_at time (when dbt command completed)
        metadata = run_results.get("metadata", {})
        generated_at = metadata.get("generated_at")

        if generated_at:
            return generated_at

        return None

    except Exception as e:
        logger.error(f"Error reading run_results.json: {e}")
        return None


def get_dbt_model_statuses() -> Dict[str, Dict[str, Any]]:
    """
    Get status and timing for each dbt model from persistent status file

    Returns:
        Dictionary mapping unique_id to {"status": str, "last_run": Optional[str]}
    """
    from dango.utils.dbt_status import get_model_statuses

    project_root = get_project_root()
    return get_model_statuses(project_root)


def get_dbt_models() -> List[Dict[str, Any]]:
    """Get list of dbt models from manifest"""
    manifest = get_dbt_manifest()

    if not manifest:
        return []

    # Get statuses and per-model timing from run_results.json
    model_statuses = get_dbt_model_statuses()

    models = []
    nodes = manifest.get("nodes", {})

    for node_id, node in nodes.items():
        # Only include models (not tests, seeds, etc.)
        if node.get("resource_type") == "model":
            schema = node.get("schema")
            model_name = node.get("name")

            # Get row count from DuckDB
            row_count = get_dbt_model_row_count(schema, model_name)

            # Get status and per-model last_run from run_results
            # If not in run_results, default to None (never run)
            model_info = model_statuses.get(node_id, {})
            status = model_info.get("status")
            last_run = model_info.get("last_run")

            models.append({
                "name": model_name,
                "unique_id": node_id,
                "path": node.get("path"),
                "materialization": node.get("config", {}).get("materialized", "view"),
                "schema": schema,
                "database": node.get("database"),
                "depends_on": node.get("depends_on", {}).get("nodes", []),
                "description": node.get("description", ""),
                "tags": node.get("tags", []),
                "row_count": row_count,
                "last_run": last_run,  # Per-model timing, not global
                "status": status,  # success/error/skipped/None
            })

    # Sort models by schema first, then by name within each schema for consistent ordering
    models.sort(key=lambda m: (m.get("schema", "").lower(), m.get("name", "").lower()))

    return models


def get_source_row_count(source_name: str) -> Optional[int]:
    """Get row count for a source from DuckDB (with timeout to prevent blocking)"""
    db_path = get_duckdb_path()

    if not db_path.exists():
        return None

    import signal
    from contextlib import contextmanager

    @contextmanager
    def timeout_context(seconds):
        """Context manager for timeout"""
        def timeout_handler(signum, frame):
            raise TimeoutError(f"Query timed out after {seconds} seconds")

        # Set the signal handler and alarm (Unix only)
        if hasattr(signal, 'SIGALRM'):
            old_handler = signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(seconds)
            try:
                yield
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
        else:
            # Windows doesn't support SIGALRM - just skip timeout
            yield

    try:
        with timeout_context(2):  # 2 second timeout
            conn = duckdb.connect(str(db_path), read_only=True)

            # Check for multi-resource schema first (raw_{source_name})
            multi_schema = f"raw_{source_name}"
            result = conn.execute(f"""
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = '{multi_schema}'
                  AND table_name NOT LIKE '_dlt_%'
                  AND table_name NOT IN ('spreadsheet', 'spreadsheet_info')
            """).fetchone()

            if result and result[0] > 0:
                # Multi-resource source: sum rows across all tables in schema
                tables = conn.execute(f"""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = '{multi_schema}'
                      AND table_name NOT LIKE '_dlt_%'
                      AND table_name NOT IN ('spreadsheet', 'spreadsheet_info')
                """).fetchall()

                total_rows = 0
                for (table_name,) in tables:
                    count_result = conn.execute(f'SELECT COUNT(*) FROM "{multi_schema}"."{table_name}"').fetchone()
                    if count_result:
                        total_rows += count_result[0]

                conn.close()
                return total_rows

            # Single-resource source: check raw.{source_name} table
            result = conn.execute(f"""
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = 'raw' AND table_name = '{source_name}'
            """).fetchone()

            if result and result[0] > 0:
                count_result = conn.execute(f'SELECT COUNT(*) FROM "raw"."{source_name}"').fetchone()
                conn.close()
                return count_result[0] if count_result else 0

            # Fall back to staging table
            result = conn.execute(f"""
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = 'staging' AND table_name = 'stg_{source_name}'
            """).fetchone()

            if result and result[0] > 0:
                count_result = conn.execute(f'SELECT COUNT(*) FROM "staging"."stg_{source_name}"').fetchone()
                conn.close()
                return count_result[0] if count_result else 0

            conn.close()
            return None

    except TimeoutError as e:
        logger.warning(f"Row count query timed out for {source_name} (database likely busy with sync)")
        return None
    except Exception as e:
        logger.error(f"Error getting row count for {source_name}: {e}")
        return None


def get_source_tables_info(source_name: str) -> Optional[Dict[str, Any]]:
    """
    Get detailed table information for a source, including per-table breakdown.

    All sources use raw_{source_name} schema pattern (industry best practice).

    Returns:
        Dictionary with:
        - total_rows: Total row count across all tables
        - tables: List of {name, row_count, schema} for each table
        - has_multiple_tables: Whether source has multiple tables

    Returns None if source not found or database unavailable.
    """
    db_path = get_duckdb_path()

    if not db_path.exists():
        return None

    try:
        conn = duckdb.connect(str(db_path), read_only=True)

        # All sources use raw_{source_name} schema
        schema_name = f"raw_{source_name}"
        result = conn.execute(f"""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema = '{schema_name}'
              AND table_name NOT LIKE '_dlt_%'
              AND table_name NOT IN ('spreadsheet', 'spreadsheet_info')
        """).fetchone()

        if result and result[0] > 0:
            # Get per-table breakdown
            tables_result = conn.execute(f"""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = '{schema_name}'
                  AND table_name NOT LIKE '_dlt_%'
                  AND table_name NOT IN ('spreadsheet', 'spreadsheet_info')
                ORDER BY table_name
            """).fetchall()

            tables = []
            total_rows = 0
            for (table_name,) in tables_result:
                count_result = conn.execute(f'SELECT COUNT(*) FROM "{schema_name}"."{table_name}"').fetchone()
                if count_result:
                    row_count = count_result[0]
                    total_rows += row_count
                    tables.append({
                        "name": table_name,
                        "row_count": row_count,
                        "schema": schema_name
                    })

            conn.close()
            return {
                "total_rows": total_rows,
                "tables": tables,
                "has_multiple_tables": len(tables) > 1
            }

        # Fall back to staging table (for backward compatibility)
        result = conn.execute(f"""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema = 'staging' AND table_name = 'stg_{source_name}'
        """).fetchone()

        if result and result[0] > 0:
            count_result = conn.execute(f'SELECT COUNT(*) FROM "staging"."stg_{source_name}"').fetchone()
            row_count = count_result[0] if count_result else 0
            conn.close()
            return {
                "total_rows": row_count,
                "tables": [{
                    "name": f"stg_{source_name}",
                    "row_count": row_count,
                    "schema": "staging"
                }],
                "has_multiple_tables": False
            }

        conn.close()
        return None

    except Exception as e:
        logger.error(f"Error getting table info for {source_name}: {e}")
        return None


def get_last_sync_time(source_name: str) -> Optional[str]:
    """Get last sync time from sync history"""
    history = load_sync_history(source_name, limit=1)
    if history and len(history) > 0:
        return history[0].get('timestamp')
    return None


def get_last_sync_status(source_name: str) -> Optional[str]:
    """Get last sync status from sync history (success/failed)"""
    history = load_sync_history(source_name, limit=1)
    if history and len(history) > 0:
        return history[0].get('status')  # 'success' or 'failed'
    return None


def mask_sensitive_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Mask sensitive fields in configuration"""
    masked_config = config.copy()

    # List of sensitive field names to mask
    sensitive_fields = {
        'password', 'api_key', 'secret', 'token', 'credentials',
        'access_token', 'refresh_token', 'private_key', 'client_secret'
    }

    def mask_dict(d: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively mask sensitive fields in dict"""
        result = {}
        for key, value in d.items():
            key_lower = key.lower()
            if any(sensitive in key_lower for sensitive in sensitive_fields):
                # Mask the value
                if isinstance(value, str) and len(value) > 4:
                    result[key] = value[:2] + '*' * (len(value) - 4) + value[-2:]
                else:
                    result[key] = '****'
            elif isinstance(value, dict):
                result[key] = mask_dict(value)
            elif isinstance(value, list):
                result[key] = [mask_dict(item) if isinstance(item, dict) else item for item in value]
            else:
                result[key] = value
        return result

    return mask_dict(masked_config)


def load_sync_history(source_name: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Load sync history for a source"""
    from dango.utils.sync_history import load_sync_history as load_history
    return load_history(get_project_root(), source_name, limit)


def save_sync_history_entry(source_name: str, entry: Dict[str, Any]):
    """Save a sync history entry for a source"""
    from dango.utils.sync_history import save_sync_history_entry as save_entry
    save_entry(get_project_root(), source_name, entry)


def get_source_freshness(source_name: str) -> Dict[str, Any]:
    """
    Calculate data freshness for a source

    Returns freshness status based on time since last successful sync:
    - synced: successful sync
    - never_synced: no sync history
    - failed: last sync failed

    Args:
        source_name: Name of the source

    Returns:
        Dictionary with freshness information:
        {
            "status": "synced" | "never_synced" | "failed",
            "hours_since_sync": float | None,
            "last_sync_time": str | None,
            "last_sync_status": str | None
        }
    """
    history = load_sync_history(source_name, limit=1)

    if not history:
        return {
            "status": "never_synced",
            "hours_since_sync": None,
            "last_sync_time": None,
            "last_sync_status": None
        }

    last_sync = history[0]
    last_sync_status = last_sync.get("status")
    last_sync_time = last_sync.get("timestamp")

    # If last sync failed, mark as failed regardless of time
    if last_sync_status != "success":
        return {
            "status": "failed",
            "hours_since_sync": None,
            "last_sync_time": last_sync_time,
            "last_sync_status": last_sync_status
        }

    # Calculate time since last successful sync
    try:
        timestamp = datetime.fromisoformat(last_sync_time.replace('Z', '+00:00'))
        hours_ago = (datetime.now() - timestamp.replace(tzinfo=None)).total_seconds() / 3600

        # All successful syncs show as "synced"
        return {
            "status": "synced",
            "hours_since_sync": round(hours_ago, 1),
            "last_sync_time": last_sync_time,
            "last_sync_status": last_sync_status
        }

    except Exception as e:
        logger.error(f"Error calculating freshness for {source_name}: {e}")
        return {
            "status": "unknown",
            "hours_since_sync": None,
            "last_sync_time": last_sync_time,
            "last_sync_status": last_sync_status
        }


def get_logs_file() -> Path:
    """Get path to persistent logs file"""
    logs_dir = get_project_root() / ".dango" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "activity.jsonl"


def append_log_entry(log_entry: Dict[str, Any]):
    """Append a log entry to the persistent logs file"""
    from dango.utils.activity_log import log_activity

    try:
        log_activity(
            project_root=get_project_root(),
            level=log_entry.get("level", "info"),
            source=log_entry.get("source", "system"),
            message=log_entry.get("message", ""),
            timestamp=log_entry.get("timestamp")
        )
    except Exception as e:
        logger.error(f"Error appending log entry: {e}")


def load_all_logs(limit: int = 1000) -> List[Dict[str, Any]]:
    """Load all logs from the persistent file"""
    logs_file = get_logs_file()

    if not logs_file.exists():
        return []

    try:
        logs = []
        with open(logs_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        logs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        # Return most recent logs first, limited to 'limit'
        return logs[-limit:][::-1]

    except Exception as e:
        logger.error(f"Error loading logs: {e}")
        return []


def check_service_via_http(service_name: str) -> str:
    """Check service via HTTP health endpoint (faster on Windows)"""
    import httpx

    # Map service names to their health check URLs
    health_urls = {
        "metabase": "http://localhost:3000/api/health",
        "dbt-docs": "http://localhost:8081"
    }

    url = health_urls.get(service_name)
    if not url:
        return "unknown"

    try:
        response = httpx.get(url, timeout=5.0, follow_redirects=False)
        if response.status_code in [200, 302]:  # 302 for dbt-docs redirect
            return "running"
        else:
            return "stopped"
    except Exception:
        return "not_found"


def check_service_via_docker(service_name: str) -> str:
    """Check service via Docker command (fast on Mac/Linux)"""
    import subprocess

    try:
        result = subprocess.run(
            ['docker', 'ps', '--filter', f'name={service_name}', '--format', '{{.Status}}'],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0 and result.stdout.strip():
            if 'Up' in result.stdout:
                return "running"
            else:
                return "stopped"
        else:
            return "not_found"
    except Exception as e:
        logger.error(f"Error checking service {service_name}: {e}")
        return "unknown"


async def check_service_status_async(service_name: str) -> str:
    """
    Check if a service is running.

    Windows: Uses HTTP health checks (Docker Desktop too slow)
    Mac/Linux: Uses Docker commands (fast and reliable)
    """
    import sys

    if sys.platform == 'win32':
        # Windows: HTTP checks are much faster
        return await asyncio.to_thread(check_service_via_http, service_name)
    else:
        # Mac/Linux: Docker commands work well
        return await asyncio.to_thread(check_service_via_docker, service_name)


# API Endpoints

@app.get("/api/status", response_model=ServiceHealth)
async def get_status():
    """
    Get service health status

    Returns health check for Dango API and related services (DuckDB, Metabase, dbt-docs)
    """
    project_root = get_project_root()
    duckdb_path = get_duckdb_path()

    # Check DuckDB
    duckdb_status = "healthy" if duckdb_path.exists() else "not_initialized"

    # Check Docker services asynchronously (run in parallel)
    metabase_status_task = asyncio.create_task(check_service_status_async("metabase"))
    dbt_docs_status_task = asyncio.create_task(check_service_status_async("dbt-docs"))

    metabase_status = await metabase_status_task
    dbt_docs_status = await dbt_docs_status_task

    return ServiceHealth(
        status="healthy",
        dango_version="0.1.0",
        services={
            "api": "running",
            "duckdb": duckdb_status,
            "metabase": metabase_status,
            "dbt_docs": dbt_docs_status
        },
        uptime="N/A"  # TODO: Track actual uptime
    )


@app.get("/api/watcher/status", response_model=WatcherStatus)
async def get_watcher_status_api():
    """
    Get file watcher status

    Returns information about the file watcher including:
    - Whether it's running
    - PID if running
    - Configuration (auto-sync, auto-dbt, debounce, patterns, directories)
    - Log file location
    """
    from dango.cli.utils import get_watcher_status
    from dango.config import ConfigLoader

    project_root = get_project_root()

    # Get watcher process status
    watcher_status = get_watcher_status(project_root)

    # Load config for settings
    try:
        loader = ConfigLoader(project_root)
        config = loader.load_config()
        platform = config.platform

        return WatcherStatus(
            running=watcher_status["running"],
            pid=watcher_status.get("pid"),
            auto_sync_enabled=platform.auto_sync,
            auto_dbt_enabled=platform.auto_dbt,
            debounce_seconds=platform.debounce_seconds,
            watch_patterns=platform.watch_patterns,
            watch_directories=platform.watch_directories,
            log_file=str(watcher_status["log_file"]) if watcher_status["log_file"] else None
        )
    except Exception as e:
        logger.error(f"Failed to load watcher config: {e}")
        # Return minimal status if config fails
        return WatcherStatus(
            running=watcher_status["running"],
            pid=watcher_status.get("pid"),
            auto_sync_enabled=False,
            auto_dbt_enabled=False,
            debounce_seconds=600,
            watch_patterns=["*.csv"],
            watch_directories=["data/uploads"],
            log_file=str(watcher_status["log_file"]) if watcher_status["log_file"] else None
        )


@app.get("/api/config")
async def get_config():
    """
    Get Dango configuration (ports, URLs, etc.)

    Returns configuration needed by the frontend to build dynamic URLs
    """
    from dango.config import ConfigLoader

    try:
        config_loader = ConfigLoader(get_project_root())
        config = config_loader.load_config()

        web_port = config.platform.port
        project_name = config.project.name
        organization = getattr(config.project, 'organization', None)

        return {
            "web_port": web_port,
            "web_url": f"http://localhost:{web_port}",
            "metabase_url": "http://localhost:3000",
            "dbt_docs_url": "http://localhost:8081",
            "api_url": f"http://localhost:{web_port}/api",
            "project_name": project_name,
            "organization": organization
        }
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        # Return defaults if config fails to load
        return {
            "web_port": 8800,
            "web_url": "http://localhost:8800",
            "metabase_url": "http://localhost:3000",
            "dbt_docs_url": "http://localhost:8081",
            "api_url": "http://localhost:8800/api",
            "project_name": "Unknown Project",
            "organization": None
        }


@app.get("/api/metabase-config")
async def get_metabase_config():
    """
    Get Metabase configuration including database ID

    Returns:
        Dictionary with Metabase configuration
    """
    try:
        metabase_yml_path = get_project_root() / ".dango" / "metabase.yml"

        if not metabase_yml_path.exists():
            return {"database_id": None, "configured": False}

        with open(metabase_yml_path, 'r', encoding='utf-8') as f:
            import yaml
            metabase_config = yaml.safe_load(f)

        database_id = metabase_config.get('database', {}).get('id')

        return {
            "database_id": database_id,
            "configured": True
        }
    except Exception as e:
        logger.error(f"Failed to load Metabase config: {e}")
        return {"database_id": None, "configured": False}


async def get_platform_health_data():
    """
    Helper function to gather platform health data (runs blocking operations in thread pool)
    """
    from dango.utils.db_health import check_duckdb_health, get_disk_usage_summary
    from dango.config import ConfigLoader

    project_root = get_project_root()
    duckdb_path = get_duckdb_path()

    # Run all blocking operations concurrently in thread pool
    db_health_task = asyncio.create_task(
        asyncio.to_thread(lambda: check_duckdb_health(duckdb_path) if duckdb_path.exists() else {
            "size_gb": 0,
            "size_mb": 0,
            "tables": 0,
            "status": "new",
            "raw_tables": 0,
            "staging_tables": 0,
            "marts_tables": 0
        })
    )

    disk_task = asyncio.create_task(asyncio.to_thread(get_disk_usage_summary, project_root))
    sources_task = asyncio.create_task(asyncio.to_thread(load_sources_config))

    # Wait for all tasks
    try:
        db_health = await db_health_task
    except Exception as e:
        logger.error(f"Error checking DB health: {e}")
        db_health = {
            "size_gb": 0,
            "size_mb": 0,
            "tables": 0,
            "status": "error",
            "raw_tables": 0,
            "staging_tables": 0,
            "marts_tables": 0
        }

    disk = await disk_task
    sources_config = await sources_task

    # Check for failed syncs
    failed_syncs = []
    for source in sources_config:
        source_name = source.get('name', 'unknown')
        history = await asyncio.to_thread(load_sync_history, source_name, 5)

        if history and len(history) > 0:
            most_recent = history[0]
            if most_recent.get("status") == "failed":
                try:
                    timestamp = datetime.fromisoformat(most_recent.get("timestamp", "").replace('Z', '+00:00'))
                    hours_ago = (datetime.now() - timestamp.replace(tzinfo=None)).total_seconds() / 3600

                    if hours_ago < 24:
                        failed_syncs.append({
                            "source": source_name,
                            "count": 1,
                            "last_error": most_recent.get("error_message", "Unknown error")
                        })
                except:
                    pass

    # Check for failed dbt runs
    failed_dbt = []
    run_results_path = project_root / "dbt" / "target" / "run_results.json"

    if run_results_path.exists():
        try:
            def read_dbt_results():
                with open(run_results_path, 'r', encoding='utf-8') as f:
                    return json.load(f)

            run_results = await asyncio.to_thread(read_dbt_results)

            results = run_results.get("results", [])
            failed_models = [r for r in results if r.get("status") == "error"]

            if failed_models:
                failed_dbt.append({
                    "run_time": run_results.get("metadata", {}).get("generated_at"),
                    "failed_models": len(failed_models),
                    "models": [r.get("unique_id", "unknown") for r in failed_models[:5]]
                })
        except Exception as e:
            logger.error(f"Error reading dbt run results: {e}")

    return {
        "db_health": db_health,
        "disk": disk,
        "sources_config": sources_config,
        "failed_syncs": failed_syncs,
        "failed_dbt": failed_dbt
    }


@app.get("/api/health/platform")
async def get_platform_health():
    """
    Get comprehensive platform health status

    Returns:
        Platform health including DB size, disk space, recent failures, and overall status
    """
    # Gather health data asynchronously
    data = await get_platform_health_data()

    db_health = data["db_health"]
    disk = data["disk"]
    sources_config = data["sources_config"]
    failed_syncs = data["failed_syncs"]
    failed_dbt = data["failed_dbt"]

    # Determine overall status
    critical_issues = []
    warnings = []

    if disk["status"] == "critical":
        critical_issues.append("Critical disk space")
    elif disk["status"] == "warning":
        warnings.append("Low disk space")

    if db_health["status"] == "critical":
        warnings.append("Very large database")
    elif db_health["status"] == "large":
        warnings.append("Large database")

    if failed_syncs:
        warnings.append(f"{len(failed_syncs)} source(s) with recent failures")

    if failed_dbt:
        warnings.append("dbt run failures")

    if critical_issues:
        overall_status = "critical"
    elif warnings:
        overall_status = "warning"
    else:
        overall_status = "healthy"

    return {
        "status": overall_status,
        "timestamp": datetime.now().isoformat(),
        "database": db_health,
        "disk": disk,
        "sync_failures": failed_syncs,
        "dbt_failures": failed_dbt,
        "total_sources": len(sources_config),
        "enabled_sources": len([s for s in sources_config if s.get("enabled", True)]),
        "critical_issues": critical_issues,
        "warnings": warnings
    }


async def get_source_status_data(source: dict) -> SourceStatus:
    """
    Get status data for a single source (runs blocking operations in thread pool)
    """
    source_name = source.get('name', 'unknown')
    source_type = source.get('type', 'unknown')
    enabled = source.get('enabled', True)

    # Run blocking operations in thread pool
    tables_info = await asyncio.to_thread(get_source_tables_info, source_name)
    last_sync = await asyncio.to_thread(get_last_sync_time, source_name)
    last_sync_status = await asyncio.to_thread(get_last_sync_status, source_name)
    history = await asyncio.to_thread(load_sync_history, source_name, 1)
    freshness = await asyncio.to_thread(get_source_freshness, source_name)

    # Extract row count and tables list
    if tables_info:
        row_count = tables_info['total_rows']
        # Show tables breakdown if source has multiple tables
        tables = [TableInfo(**t) for t in tables_info['tables']] if tables_info.get('has_multiple_tables') else None
    else:
        row_count = None
        tables = None

    rows_processed = history[0].get('rows_processed', 0) if history else None

    # Determine status (priority: failed > synced > empty > not_synced)
    if last_sync_status == "failed":
        status = "failed"
    elif not history:
        # Never synced - no history at all
        status = "not_synced"
    elif last_sync_status == "success" and (rows_processed == 0 or row_count == 0 or row_count is None):
        # Synced but no data loaded
        status = "empty"
    elif row_count is not None and row_count > 0:
        # Has data
        status = "synced"
    else:
        # Edge case
        status = "not_synced"

    return SourceStatus(
        name=source_name,
        type=source_type,
        enabled=enabled,
        last_sync=last_sync,
        row_count=row_count,
        status=status,
        freshness=freshness,
        tables=tables
    )


@app.get("/api/sources", response_model=List[SourceStatus])
async def get_sources():
    """
    List all configured data sources with status

    Returns:
        List of sources with sync status, row counts, and timestamps
    """
    # Load sources config in thread pool
    sources_config = await asyncio.to_thread(load_sources_config)

    # Process all sources concurrently
    tasks = [get_source_status_data(source) for source in sources_config]
    source_statuses = await asyncio.gather(*tasks)

    # Sort sources alphabetically by name for consistent ordering
    source_statuses = sorted(source_statuses, key=lambda s: s.name.lower())

    return source_statuses


@app.get("/api/sources/{source_name}/details")
async def get_source_details(source_name: str):
    """
    Get detailed information about a specific source

    Args:
        source_name: Name of the source

    Returns:
        Source configuration (masked) and sync history
    """
    sources_config = load_sources_config()

    # Find the source
    source_config = None
    for source in sources_config:
        if source.get('name') == source_name:
            source_config = source
            break

    if not source_config:
        raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")

    # Mask sensitive data
    masked_config = mask_sensitive_config(source_config)

    # Load sync history
    history = load_sync_history(source_name, limit=20)

    # Get current stats
    row_count = get_source_row_count(source_name)

    # Get freshness information
    freshness = get_source_freshness(source_name)

    # Get table breakdown for sources with multiple tables
    tables_info = get_source_tables_info(source_name)
    tables = None
    if tables_info and tables_info.get('has_multiple_tables'):
        tables = [TableInfo(**t) for t in tables_info['tables']]

    return {
        "name": source_name,
        "config": masked_config,
        "history": history,
        "row_count": row_count,
        "freshness": freshness,
        "tables": tables
    }


@app.post("/api/sources/{source_name}/sync", response_model=SyncResponse)
async def trigger_sync(
    source_name: str,
    sync_request: SyncRequest,
    background_tasks: BackgroundTasks
):
    """
    Trigger sync for a specific source

    Args:
        source_name: Name of the source to sync
        sync_request: Sync parameters (full_refresh, date range)

    Returns:
        Sync response with status
    """
    # Verify source exists
    sources_config = load_sources_config()
    source_exists = any(s.get('name') == source_name for s in sources_config)

    if not source_exists:
        raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")

    # Start sync in background
    background_tasks.add_task(
        run_sync_task,
        source_name,
        sync_request.full_refresh,
        sync_request.start_date,
        sync_request.end_date
    )

    # Broadcast sync started event via WebSocket
    await ws_manager.broadcast({
        "event": "sync_started",
        "source": source_name,
        "timestamp": datetime.now().isoformat()
    })

    return SyncResponse(
        success=True,
        message=f"Sync started for {source_name}",
        source_name=source_name,
        started_at=datetime.now().isoformat()
    )


async def run_dbt_after_delete(source_name: str):
    """
    Run dbt models after file deletion to update staging/downstream tables

    This ensures that when files are deleted, the staging models reflect
    the current state of the raw data (with rows removed)
    """
    import time
    from dango.utils import DbtLock, DbtLockError

    start_time = time.time()
    sync_timestamp = datetime.now().isoformat()
    project_root = get_project_root()

    # Try to acquire lock before running dbt
    try:
        lock = DbtLock(
            project_root=project_root,
            source="ui",
            operation=f"dbt run after {source_name} file deletion"
        )
        lock.acquire()
    except DbtLockError as e:
        # Lock is held by another process - broadcast error and return
        error_msg = str(e).split('\n')[0]
        await ws_manager.broadcast({
            "event": "dbt_run_all_failed",
            "source": f"dbt (triggered by {source_name} delete)",
            "message": error_msg,
            "timestamp": datetime.now().isoformat()
        })
        append_log_entry({
            "timestamp": datetime.now().isoformat(),
            "level": "error",
            "source": f"dbt (triggered by {source_name} delete)",
            "message": f"dbt run blocked: {error_msg}"
        })
        logger.warning(f"Could not acquire dbt lock for delete operation: {e}")
        return

    try:
        from dango.transformation import run_dbt_models

        # Log dbt start
        append_log_entry({
            "timestamp": sync_timestamp,
            "level": "info",
            "source": f"dbt (triggered by {source_name} delete)",
            "message": f"Running dbt models for {source_name} after file deletion"
        })

        # Broadcast dbt started
        await ws_manager.broadcast({
            "event": "dbt_run_all_started",
            "source": f"dbt (triggered by {source_name} delete)",
            "message": f"Updating models after file deletion",
            "timestamp": sync_timestamp
        })

        # Run dbt for this source and downstream models
        # Use source:source_name+ to run all models dependent on this source
        select_criteria = f"source:{source_name}+"
        dbt_success, dbt_output = run_dbt_models(get_project_root(), select=select_criteria)

        # Calculate duration
        duration = time.time() - start_time

        # Get current row count
        rows_processed = get_source_row_count(source_name) or 0

        if dbt_success:
            # Log success
            append_log_entry({
                "timestamp": datetime.now().isoformat(),
                "level": "success",
                "source": f"dbt (triggered by {source_name} delete)",
                "message": f"dbt models updated successfully"
            })

            # Broadcast completion
            await ws_manager.broadcast({
                "event": "dbt_run_all_completed",
                "source": f"dbt (triggered by {source_name} delete)",
                "message": f"Models updated after file deletion",
                "timestamp": datetime.now().isoformat()
            })

            # Save sync history with success
            history_entry = {
                "timestamp": sync_timestamp,
                "status": "success",
                "duration_seconds": round(duration, 2),
                "rows_processed": rows_processed,
                "full_refresh": False,
                "error_message": None
            }
            save_sync_history_entry(source_name, history_entry)
        else:
            # Log failure - dbt_output already contains "dbt run failed:" prefix
            append_log_entry({
                "timestamp": datetime.now().isoformat(),
                "level": "error",
                "source": f"dbt (triggered by {source_name} delete)",
                "message": dbt_output  # Don't add extra "dbt run failed:" prefix
            })

            # Broadcast failure
            await ws_manager.broadcast({
                "event": "dbt_run_all_failed",
                "source": f"dbt (triggered by {source_name} delete)",
                "message": f"dbt run failed",
                "timestamp": datetime.now().isoformat()
            })

            # Save sync history with failure
            history_entry = {
                "timestamp": sync_timestamp,
                "status": "failed",
                "duration_seconds": round(duration, 2),
                "rows_processed": 0,
                "full_refresh": False,
                "error_message": dbt_output
            }
            save_sync_history_entry(source_name, history_entry)

    except Exception as e:
        logger.error(f"Error running dbt after delete: {e}")
        duration = time.time() - start_time

        append_log_entry({
            "timestamp": datetime.now().isoformat(),
            "level": "error",
            "source": f"dbt (triggered by {source_name} delete)",
            "message": f"Error running dbt: {str(e)}"
        })

        # Save sync history with exception
        history_entry = {
            "timestamp": sync_timestamp,
            "status": "failed",
            "duration_seconds": round(duration, 2),
            "rows_processed": 0,
            "full_refresh": False,
            "error_message": str(e)
        }
        save_sync_history_entry(source_name, history_entry)
    finally:
        # Always release the lock
        lock.release()


async def run_sync_task(
    source_name: str,
    full_refresh: bool,
    start_date: Optional[str],
    end_date: Optional[str]
):
    """
    Run sync task in background

    This function imports and runs the dlt sync process, broadcasting updates via WebSocket
    """
    import time
    from dango.utils import DbtLock, DbtLockError

    start_time = time.time()
    sync_timestamp = datetime.now().isoformat()
    success = False
    error_message = None
    rows_processed = 0
    project_root = get_project_root()

    # Try to acquire lock before running sync (which includes dbt)
    try:
        lock = DbtLock(
            project_root=project_root,
            source="ui",
            operation=f"sync {source_name} (includes dbt run)"
        )
        lock.acquire()
    except DbtLockError as e:
        # Lock is held by another process - broadcast error and return
        error_msg = str(e).split('\n')[0]
        await ws_manager.broadcast({
            "event": "sync_failed",
            "source": source_name,
            "message": error_msg,
            "timestamp": datetime.now().isoformat()
        })
        append_log_entry({
            "timestamp": datetime.now().isoformat(),
            "level": "error",
            "source": source_name,
            "message": f"Sync blocked: {error_msg}"
        })
        logger.warning(f"Could not acquire dbt lock for sync {source_name}: {e}")
        return

    try:
        from dango.config.loader import load_config

        # Log sync start
        append_log_entry({
            "timestamp": sync_timestamp,
            "level": "info",
            "source": source_name,
            "message": f"Starting sync for {source_name}"
        })

        # Broadcast sync started
        await ws_manager.broadcast({
            "event": "sync_started",
            "source": source_name,
            "message": f"Starting sync for {source_name}",
            "timestamp": sync_timestamp
        })

        # Load config and get source
        config = load_config(get_project_root())
        source_config = config.sources.get_source(source_name)

        if not source_config:
            raise ValueError(f"Source '{source_name}' not found in configuration")

        # Use the same run_sync function as CLI for consistent behavior
        # This ensures: data load → dbt run → docs generation → Metabase sync
        from dango.ingestion import run_sync
        from datetime import datetime as dt

        # Parse dates if provided
        start_date_obj = dt.strptime(start_date, "%Y-%m-%d") if start_date else None
        end_date_obj = dt.strptime(end_date, "%Y-%m-%d") if end_date else None

        # Log before running sync
        append_log_entry({
            "timestamp": datetime.now().isoformat(),
            "level": "info",
            "source": source_name,
            "message": "Loading data from source"
        })

        # Run sync with the complete flow (data load → dbt → docs → metabase)
        summary = run_sync(
            project_root=get_project_root(),
            sources=[source_config],
            start_date=start_date_obj,
            end_date=end_date_obj,
            full_refresh=full_refresh
        )

        success = summary["failed_count"] == 0
        # Also check that we actually have successful sources (not just zero failures)
        has_successful_sources = len(summary.get("success_sources", [])) > 0

        # Extract error message if sync failed
        if not success and summary.get("failed_sources"):
            # Get error from first failed source (there should only be one when syncing single source)
            error_message = summary["failed_sources"][0].get("error", "Unknown error")
        else:
            error_message = None

        # Log after data load
        append_log_entry({
            "timestamp": datetime.now().isoformat(),
            "level": "success" if success else "error",
            "source": source_name,
            "message": f"Data load {'completed' if success else 'failed'}" + (f": {error_message}" if error_message else "")
        })

        # Broadcast and log dbt run (which happens inside run_sync AFTER data load)
        # ONLY broadcast dbt messages if sync actually succeeded AND we have successful sources
        if success and has_successful_sources:
            # Build helpful message about what dbt will run
            dbt_message = f"Running dbt models for source: {source_name}"
            dbt_detail = f"Processing staging.{source_name} and downstream models"

            # Broadcast dbt started (happens after data load, before dbt actually runs in run_sync)
            await ws_manager.broadcast({
                "event": "dbt_run_all_started",
                "source": f"dbt (triggered by {source_name})",
                "message": dbt_message,
                "timestamp": datetime.now().isoformat()
            })
            append_log_entry({
                "timestamp": datetime.now().isoformat(),
                "level": "info",
                "source": f"dbt (triggered by {source_name})",
                "message": dbt_detail
            })

            append_log_entry({
                "timestamp": datetime.now().isoformat(),
                "level": "success",
                "source": f"dbt (triggered by {source_name})",
                "message": f"dbt models completed: staging.{source_name} and downstream"
            })

            # Broadcast dbt run completed
            await ws_manager.broadcast({
                "event": "dbt_run_all_completed",
                "source": f"dbt (triggered by {source_name})",
                "message": f"dbt models completed for {source_name}",
                "timestamp": datetime.now().isoformat()
            })

        # Get row count after sync (only if successful)
        if success:
            rows_processed = get_source_row_count(source_name) or 0

        # Calculate duration
        duration = time.time() - start_time

        # Save sync history
        history_entry = {
            "timestamp": sync_timestamp,
            "status": "success" if success else "failed",
            "duration_seconds": round(duration, 2),
            "rows_processed": rows_processed if success else 0,
            "full_refresh": full_refresh,
            "error_message": error_message
        }
        save_sync_history_entry(source_name, history_entry)

        # Log completion (conditional based on success/failure)
        if success:
            append_log_entry({
                "timestamp": datetime.now().isoformat(),
                "level": "success",
                "source": source_name,
                "message": f"Sync completed in {round(duration, 1)}s - {rows_processed:,} rows"
            })

            # Trigger Metabase schema sync to ensure new tables are discoverable
            # This matches CLI behavior (main.py:1265-1275) which calls sync_metabase_schema
            # after run_sync() as a backup in case the internal call was skipped
            from dango.visualization.metabase import sync_metabase_schema
            sync_metabase_schema(project_root)
        else:
            # For failures, log with error details
            append_log_entry({
                "timestamp": datetime.now().isoformat(),
                "level": "error",
                "source": source_name,
                "message": f"Sync failed after {round(duration, 1)}s" + (f": {error_message}" if error_message else "")
            })

        # Broadcast completion with detailed error message if failed
        await ws_manager.broadcast({
            "event": "sync_completed" if success else "sync_failed",
            "source": source_name,
            "message": "Sync completed successfully" if success else (error_message or "Sync failed"),
            "timestamp": datetime.now().isoformat(),
            "error": error_message if not success else None
        })

    except Exception as e:
        logger.error(f"Error running sync for {source_name}: {e}")
        error_message = str(e)

        # Log error
        append_log_entry({
            "timestamp": datetime.now().isoformat(),
            "level": "error",
            "source": source_name,
            "message": f"Sync failed: {error_message}"
        })

        # Calculate duration
        duration = time.time() - start_time

        # Save failed sync history
        history_entry = {
            "timestamp": sync_timestamp,
            "status": "failed",
            "duration_seconds": round(duration, 2),
            "rows_processed": 0,
            "full_refresh": full_refresh,
            "error_message": error_message
        }
        save_sync_history_entry(source_name, history_entry)

        # Broadcast error
        await ws_manager.broadcast({
            "event": "sync_failed",
            "source": source_name,
            "message": f"Sync failed: {error_message}",
            "timestamp": datetime.now().isoformat()
        })
    finally:
        # Always release the lock
        lock.release()


@app.get("/api/sources/{source_name}/logs", response_model=List[LogEntry])
async def get_source_logs(source_name: str, limit: int = 100):
    """
    Get sync logs for a specific source

    Args:
        source_name: Name of the source
        limit: Maximum number of log entries to return

    Returns:
        List of log entries
    """
    log_file = get_project_root() / "logs" / f"{source_name}_sync.log"

    if not log_file.exists():
        return []

    try:
        logs = []
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

            # Get last N lines
            for line in lines[-limit:]:
                # Parse log line (assuming format: timestamp - level - message)
                parts = line.strip().split(' - ', 2)
                if len(parts) >= 3:
                    logs.append(LogEntry(
                        timestamp=parts[0],
                        level=parts[1],
                        message=parts[2]
                    ))
                else:
                    # Fallback for unparseable lines
                    logs.append(LogEntry(
                        timestamp=datetime.now().isoformat(),
                        level="INFO",
                        message=line.strip()
                    ))

        return logs

    except Exception as e:
        logger.error(f"Error reading logs for {source_name}: {e}")
        raise HTTPException(status_code=500, detail=f"Error reading logs: {str(e)}")


@app.get("/api/logs")
async def get_all_logs(limit: int = 1000):
    """
    Get all activity logs

    Args:
        limit: Maximum number of log entries to return (default 1000)

    Returns:
        List of all log entries
    """
    try:
        logs = load_all_logs(limit=limit)
        return logs
    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching logs: {str(e)}")


@app.get("/api/dbt/models")
async def list_dbt_models():
    """
    List all dbt models

    Returns:
        List of dbt models with their metadata
    """
    try:
        models = get_dbt_models()
        return {"models": models}
    except Exception as e:
        logger.error(f"Error fetching dbt models: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching dbt models: {str(e)}")


@app.post("/api/dbt/models/{model_name}/run")
async def run_dbt_model(
    model_name: str,
    background_tasks: BackgroundTasks,
    cascade: bool = True
):
    """
    Run a specific dbt model

    Args:
        model_name: Name of the model to run
        cascade: Whether to cascade to downstream models (default True)

    Returns:
        Success message
    """
    # Check if model exists (use manifest only, avoid DuckDB query which can block)
    manifest = get_dbt_manifest()
    if manifest:
        nodes = manifest.get("nodes", {})
        model_exists = any(
            node.get("resource_type") == "model" and node.get("name") == model_name
            for node in nodes.values()
        )
    else:
        model_exists = False

    if not model_exists:
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")

    # Broadcast start
    await ws_manager.broadcast({
        "event": "dbt_run_started",
        "source": f"dbt:{model_name}",
        "message": f"Running dbt model: {model_name}",
        "timestamp": datetime.now().isoformat()
    })

    # Run in background
    background_tasks.add_task(
        run_dbt_model_task,
        model_name,
        cascade
    )

    return {
        "success": True,
        "message": f"dbt model '{model_name}' run started",
        "model_name": model_name,
        "started_at": datetime.now().isoformat()
    }


async def run_dbt_model_task(model_name: str, cascade: bool):
    """Run dbt model in background"""
    import subprocess
    import time
    import sys
    from pathlib import Path as PathLib
    from dango.utils import DbtLock, DbtLockError
    from dango.utils.dbt_status import update_model_status

    start_time = time.time()
    project_root = get_project_root()
    dbt_dir = project_root / "dbt"

    # Try to acquire lock before running dbt
    try:
        lock = DbtLock(
            project_root=project_root,
            source="ui",
            operation=f"dbt run {model_name}{'+ (cascade)' if cascade else ''}"
        )
        lock.acquire()
    except DbtLockError as e:
        # Lock is held by another process - broadcast error and return
        await ws_manager.broadcast({
            "event": "dbt_run_failed",
            "source": f"dbt:{model_name}",
            "message": str(e).split('\n')[0],  # First line of error message
            "timestamp": datetime.now().isoformat()
        })
        logger.warning(f"Could not acquire dbt lock for {model_name}: {e}")
        return

    # Get dbt executable path (from venv or system PATH)
    python_bin_dir = PathLib(sys.executable).parent
    dbt_path = python_bin_dir / "dbt"
    dbt_cmd = str(dbt_path) if dbt_path.exists() else "dbt"

    try:
        # Build the dbt command
        if cascade:
            # Run model and all downstream models
            cmd = [dbt_cmd, "run", "--select", f"{model_name}+", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)]
        else:
            # Run only this model
            cmd = [dbt_cmd, "run", "--select", model_name, "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)]

        # Broadcast progress
        await ws_manager.broadcast({
            "event": "dbt_run_progress",
            "source": f"dbt:{model_name}",
            "message": f"Executing: {' '.join(cmd)}",
            "timestamp": datetime.now().isoformat()
        })

        # Run dbt
        result = subprocess.run(
            cmd,
            cwd=str(dbt_dir),
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        duration = time.time() - start_time

        if result.returncode == 0:
            # Update persistent model status
            update_model_status(project_root)

            # Success
            await ws_manager.broadcast({
                "event": "dbt_run_completed",
                "source": f"dbt:{model_name}",
                "message": f"Model '{model_name}' ran successfully in {duration:.1f}s",
                "timestamp": datetime.now().isoformat()
            })

            # CRITICAL: Refresh Metabase connection to see new/updated tables
            from dango.visualization.metabase import refresh_metabase_connection
            project_root = get_project_root()

            if refresh_metabase_connection(project_root):
                await ws_manager.broadcast({
                    "event": "dbt_run_progress",
                    "source": f"dbt:{model_name}",
                    "message": "Metabase connection refreshed",
                    "timestamp": datetime.now().isoformat()
                })
        else:
            # Failed
            error_msg = result.stderr or result.stdout or "Unknown error"
            await ws_manager.broadcast({
                "event": "dbt_run_failed",
                "source": f"dbt:{model_name}",
                "message": f"Model '{model_name}' failed: {error_msg[:200]}",
                "timestamp": datetime.now().isoformat()
            })

    except subprocess.TimeoutExpired:
        await ws_manager.broadcast({
            "event": "dbt_run_failed",
            "source": f"dbt:{model_name}",
            "message": f"Model '{model_name}' timed out after 5 minutes",
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Error running dbt model {model_name}: {e}")
        await ws_manager.broadcast({
            "event": "dbt_run_failed",
            "source": f"dbt:{model_name}",
            "message": f"Model '{model_name}' failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        })
    finally:
        # Always release the lock
        lock.release()


@app.post("/api/sources/{source_name}/upload-csv")
async def upload_csv_to_source(
    source_name: str,
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None,
    trigger_sync: bool = False  # Default to false - let frontend control when to sync
):
    """
    Upload a CSV file to an existing pre-configured CSV source

    By default, this only saves the file to disk without triggering sync.
    This allows batch uploads to complete quickly, then trigger ONE sync at the end.

    Args:
        source_name: Name of the existing CSV source (from path)
        file: CSV file to upload
        trigger_sync: If True, immediately trigger sync after upload (default: False)

    Returns:
        Success message and file info
    """
    try:
        import aiofiles

        project_root = get_project_root()

        # Load sources configuration
        sources_file = project_root / ".dango" / "sources.yml"
        if not sources_file.exists():
            raise HTTPException(status_code=404, detail="No sources configured")

        with open(sources_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}

        # Find the source
        source_config = None
        for source in config.get('sources', []):
            if source.get('name') == source_name:
                source_config = source
                break

        if not source_config:
            raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")

        # Validate source is of type CSV
        if source_config.get('type') != 'csv':
            raise HTTPException(
                status_code=400,
                detail=f"Source '{source_name}' is not a CSV source (type: {source_config.get('type')})"
            )

        # Validate file type
        if not file.filename.endswith('.csv'):
            raise HTTPException(status_code=400, detail="Only CSV files are supported")

        # Get directory from source config
        csv_config = source_config.get('csv', {})
        directory = csv_config.get('directory', 'data')

        # Resolve directory path (could be relative or absolute)
        data_dir = Path(directory)
        if not data_dir.is_absolute():
            data_dir = project_root / directory

        # Create data directory if it doesn't exist
        data_dir.mkdir(parents=True, exist_ok=True)

        # Check if file already exists
        file_path = data_dir / file.filename
        if file_path.exists():
            raise HTTPException(
                status_code=409,
                detail=f"File '{file.filename}' already exists. Delete the existing file first or rename your file."
            )
        async with aiofiles.open(file_path, "wb") as buffer:
            content = await file.read()
            await buffer.write(content)

        logger.info(f"Uploaded CSV file for source '{source_name}': {file_path}")

        # Broadcast upload event via WebSocket
        await ws_manager.broadcast({
            "event": "csv_uploaded",
            "source": source_name,
            "message": f"CSV file {file.filename} uploaded to {source_name}",
            "timestamp": datetime.now().isoformat()
        })

        # Only trigger sync if explicitly requested (for batch upload optimization)
        if trigger_sync and background_tasks:
            background_tasks.add_task(
                run_sync_task,
                source_name,
                full_refresh=False,
                start_date=None,
                end_date=None
            )
            logger.info(f"Triggered immediate sync for source '{source_name}'")

        return {
            "success": True,
            "message": f"CSV uploaded successfully: {file.filename}" + (" - Sync started." if trigger_sync else ""),
            "source_name": source_name,
            "file_path": str(file_path),
            "file_name": file.filename,
            "auto_sync": trigger_sync
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading CSV: {e}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@app.get("/api/sources/{source_name}/csv-files")
async def get_csv_files(source_name: str):
    """
    Get CSV files for a source - both on disk and loaded into database

    Args:
        source_name: Name of the CSV source

    Returns:
        List of files with their status (on_disk, loaded, both)
    """
    try:
        project_root = get_project_root()

        # Load sources configuration
        sources_file = project_root / ".dango" / "sources.yml"
        if not sources_file.exists():
            raise HTTPException(status_code=404, detail="No sources configured")

        with open(sources_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}

        # Find the source
        source_config = None
        for source in config.get('sources', []):
            if source.get('name') == source_name:
                source_config = source
                break

        if not source_config:
            raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")

        # Validate source is of type CSV
        if source_config.get('type') != 'csv':
            raise HTTPException(
                status_code=400,
                detail=f"Source '{source_name}' is not a CSV source"
            )

        csv_config = source_config.get('csv', {})
        directory = csv_config.get('directory', 'data')
        file_pattern = csv_config.get('file_pattern', '*.csv')

        # Resolve directory path
        if not Path(directory).is_absolute():
            directory = project_root / directory
        else:
            directory = Path(directory)

        # Get files on disk
        files_on_disk = {}
        if directory.exists():
            import glob
            pattern = str(directory / file_pattern)
            for filepath in glob.glob(pattern):
                filename = os.path.basename(filepath)
                stat = os.stat(filepath)
                files_on_disk[filepath] = {
                    'filename': filename,
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'path': filepath
                }

        # Get files from metadata table
        files_loaded = {}
        duckdb_path = get_duckdb_path()
        if duckdb_path.exists():
            import duckdb
            conn = duckdb.connect(str(duckdb_path))

            # Check if metadata table exists
            result = conn.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_name = '_dango_file_metadata'
            """).fetchall()

            if result:
                # Get loaded files for this source
                # Include deleted files only if loaded within last 7 days
                rows = conn.execute("""
                    SELECT file_path, rows_loaded, loaded_at, status
                    FROM _dango_file_metadata
                    WHERE source_name = ?
                    AND (status != 'deleted' OR loaded_at > NOW() - INTERVAL 7 DAY)
                    ORDER BY loaded_at DESC
                """, [source_name]).fetchall()

                for row in rows:
                    filepath, rows_loaded, loaded_at, status = row
                    filename = os.path.basename(filepath)
                    files_loaded[filepath] = {
                        'filename': filename,
                        'rows_loaded': rows_loaded,
                        'loaded_at': loaded_at.isoformat() if loaded_at else None,
                        'status': status,
                        'path': filepath
                    }

            conn.close()

        # Combine information
        all_files = []

        # Files on disk
        for filepath, info in files_on_disk.items():
            file_info = {
                'filename': info['filename'],
                'path': filepath,
                'size': info['size'],
                'modified': info['modified'],
                'on_disk': True,
                'loaded': filepath in files_loaded
            }

            if filepath in files_loaded:
                file_info['rows_loaded'] = files_loaded[filepath]['rows_loaded']
                file_info['loaded_at'] = files_loaded[filepath]['loaded_at']
                file_info['status'] = files_loaded[filepath]['status']

            all_files.append(file_info)

        # Files in database but not on disk (deleted)
        for filepath, info in files_loaded.items():
            if filepath not in files_on_disk:
                all_files.append({
                    'filename': info['filename'],
                    'path': filepath,
                    'size': None,
                    'modified': None,
                    'on_disk': False,
                    'loaded': True,
                    'rows_loaded': info['rows_loaded'],
                    'loaded_at': info['loaded_at'],
                    'status': 'file_deleted'
                })

        return {
            'source_name': source_name,
            'directory': str(directory),
            'file_pattern': file_pattern,
            'files': all_files,
            'total_files': len(all_files),
            'files_on_disk': len(files_on_disk),
            'files_loaded': len(files_loaded)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting CSV files: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get CSV files: {str(e)}")


@app.delete("/api/sources/{source_name}/csv-files")
async def delete_csv_file(
    source_name: str,
    file_path: str = Query(..., description="Full path to file to delete"),
    background_tasks: BackgroundTasks = None
):
    """
    Delete a CSV file from filesystem and trigger sync to update database

    VERSION: 2025-11-04-v2 (immediate DB cleanup, delete metadata record)

    Args:
        source_name: Name of the CSV source
        file_path: Full path to the file to delete
        background_tasks: FastAPI background tasks

    Returns:
        Success message with deletion details
    """
    import os
    from pathlib import Path

    logger.info(f"🔴 DELETE ENDPOINT CALLED - VERSION 2025-11-04-v2 - source: {source_name}, file: {file_path}")

    try:
        project_root = get_project_root()
        sources_config = load_sources_config()

        # Find source config
        source_config = next((s for s in sources_config if s.get('name') == source_name), None)
        if not source_config:
            raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")

        # Verify source is CSV type
        if source_config.get('type') != 'csv':
            raise HTTPException(status_code=400, detail=f"Source '{source_name}' is not a CSV source")

        # Verify file exists
        file_to_delete = Path(file_path)
        if not file_to_delete.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

        # Verify file is in the source directory (security check)
        source_directory = source_config.get('csv', {}).get('directory')
        if not source_directory:
            raise HTTPException(status_code=500, detail="Source directory not configured")

        # Resolve source directory relative to project root
        source_dir = project_root / source_directory
        source_dir = source_dir.resolve()

        try:
            file_to_delete.resolve().relative_to(source_dir)
        except ValueError:
            raise HTTPException(status_code=403, detail="Cannot delete files outside source directory")

        # Delete data from database FIRST (before deleting file)
        # This ensures data is cleaned up immediately and user sees instant feedback
        filename = file_to_delete.name
        logger.info(f"🔴 Starting DB cleanup for file: {filename}")

        # Connect to DuckDB (reuse connection for both data and metadata cleanup)
        import duckdb
        from dango.config import ConfigLoader

        loader = ConfigLoader(project_root)
        config = loader.load_config()
        duckdb_path = project_root / config.platform.duckdb_path
        duckdb_path_resolved = duckdb_path.resolve()
        logger.info(f"🔴 DuckDB path (config): {config.platform.duckdb_path}")
        logger.info(f"🔴 DuckDB path (resolved): {duckdb_path_resolved}")
        logger.info(f"🔴 File exists: {duckdb_path_resolved.exists()}")
        logger.info(f"🔴 File size: {duckdb_path_resolved.stat().st_size if duckdb_path_resolved.exists() else 'N/A'}")

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
            logger.info(f"🔴 Tables in database: {all_tables}")

            # Delete rows for this file from raw table (if table exists)
            target_table = f"raw.{source_name}"
            logger.info(f"🔴 Deleting from {target_table} WHERE _dango_filename = '{filename}'")

            # Check if table exists first
            table_exists = conn.execute(f"""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema = 'raw' AND table_name = '{source_name}'
            """).fetchone()[0] > 0

            if table_exists:
                result = conn.execute(f"DELETE FROM {target_table} WHERE _dango_filename = ?", [filename])
                rows_deleted = conn.execute("SELECT changes()").fetchone()[0]
                logger.info(f"🔴 Deleted {rows_deleted} rows from {target_table}")
            else:
                logger.warning(f"🔴 Table {target_table} doesn't exist - file was never synced. Skipping data deletion.")
                rows_deleted = 0

        except Exception as e:
            logger.error(f"🔴 ERROR during DB data deletion: {type(e).__name__}: {str(e)}")
            import traceback
            logger.error(f"🔴 Traceback: {traceback.format_exc()}")
            # Continue with metadata cleanup even if data deletion fails

        # ALWAYS delete metadata record (even if data deletion failed)
        # Reuse the same connection
        try:
            if conn is None:
                logger.error("🔴 Connection is None - cannot clean up metadata")
            else:
                logger.info(f"🔴 Deleting metadata record for source={source_name}, file_path={file_path}")
                conn.execute(
                    """
                    DELETE FROM _dango_file_metadata
                    WHERE source_name = ? AND file_path = ?
                    """,
                    [source_name, file_path]
                )
                metadata_deleted = conn.execute("SELECT changes()").fetchone()[0]
                logger.info(f"🔴 Deleted {metadata_deleted} metadata records")
                logger.info(f"🔴 Metadata cleanup complete for file: {filename}")

        except Exception as e:
            logger.error(f"🔴 ERROR during metadata cleanup: {type(e).__name__}: {str(e)}")
            import traceback
            logger.error(f"🔴 Traceback: {traceback.format_exc()}")

        finally:
            # Always close connection
            if conn is not None:
                try:
                    conn.close()
                    logger.info(f"🔴 DB connection closed")
                except:
                    pass

        # Delete file from filesystem
        os.remove(file_path)
        logger.info(f"Deleted file: {file_path}")

        # Broadcast deletion event
        await ws_manager.broadcast({
            "event": "csv_deleted",
            "source": source_name,
            "message": f"Deleted {filename}",
            "timestamp": datetime.now().isoformat()
        })

        # Log activity
        append_log_entry({
            "timestamp": datetime.now().isoformat(),
            "level": "info",
            "source": source_name,
            "message": f"Deleted file: {filename}"
        })

        # Trigger dbt run for downstream models (in background)
        if background_tasks:
            background_tasks.add_task(run_dbt_after_delete, source_name)
            logger.info(f"Triggered dbt run for {source_name} after file deletion")

        return {
            "success": True,
            "message": f"File deleted: {filename}",
            "file_path": file_path,
            "source_name": source_name,
            "background_sync": True
        }

    except HTTPException as he:
        logger.error(f"🔴 HTTP Exception during delete: {he.status_code} - {he.detail}")
        raise
    except Exception as e:
        logger.error(f"🔴 UNEXPECTED ERROR deleting CSV file: {type(e).__name__}: {str(e)}")
        import traceback
        logger.error(f"🔴 Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time updates

    Clients can connect to receive real-time updates about:
    - Sync progress and completion
    - Errors and warnings
    - Data freshness changes
    """
    await ws_manager.connect(websocket)

    try:
        # Send welcome message
        await websocket.send_json({
            "event": "connected",
            "message": "Connected to Dango real-time updates",
            "timestamp": datetime.now().isoformat()
        })

        # Keep connection alive and listen for client messages
        while True:
            # Wait for messages from client (ping/pong for keepalive)
            data = await websocket.receive_text()

            # Echo back for now (can add client commands later)
            await websocket.send_json({
                "event": "echo",
                "data": data,
                "timestamp": datetime.now().isoformat()
            })

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        logger.info("Client disconnected from WebSocket")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)


# Root endpoint - Serve dashboard UI
def _inject_version(html_content: str) -> str:
    """Replace version placeholder with actual dango version."""
    return html_content.replace("{{DANGO_VERSION}}", dango.__version__)


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the dashboard UI"""
    index_file = Path(__file__).parent / "static" / "index.html"

    if index_file.exists():
        return _inject_version(index_file.read_text(encoding='utf-8'))
    else:
        # Fallback if static files not found
        return """
        <html>
            <head><title>Dango - Setup Required</title></head>
            <body>
                <h1>Dango Web UI</h1>
                <p>Static files not found. Please ensure the installation is complete.</p>
                <p>API documentation available at: <a href="/api/docs">/api/docs</a></p>
            </body>
        </html>
        """


# Health page
@app.get("/health", response_class=HTMLResponse)
async def health_page():
    """Serve the platform health page"""
    health_file = Path(__file__).parent / "static" / "health.html"

    if health_file.exists():
        return _inject_version(health_file.read_text(encoding='utf-8'))
    else:
        return "<html><body><h1>Health page not found</h1></body></html>"


# Logs page
@app.get("/logs", response_class=HTMLResponse)
async def logs_page():
    """Serve the logs page"""
    logs_file = Path(__file__).parent / "static" / "logs.html"

    if logs_file.exists():
        return _inject_version(logs_file.read_text(encoding='utf-8'))
    else:
        return """
        <html>
            <head><title>Logs - Dango</title></head>
            <body>
                <h1>Logs Page Not Found</h1>
                <p><a href="/">Back to Dashboard</a></p>
            </body>
        </html>
        """


# API info endpoint
@app.get("/api")
async def api_info():
    """API information endpoint"""
    return {
        "message": "Dango API",
        "version": "0.1.0",
        "docs": "/api/docs",
        "websocket": "/ws"
    }


# API docs without custom navbar (just default Swagger UI)
@app.get("/api/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    """Swagger UI (default, no custom navbar)"""
    from fastapi.openapi.docs import get_swagger_ui_html
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="Dango API - Documentation"
    )


@app.get("/api/redoc", include_in_schema=False)
async def custom_redoc_html():
    """ReDoc (default, no custom navbar)"""
    from fastapi.openapi.docs import get_redoc_html
    return get_redoc_html(
        openapi_url="/openapi.json",
        title="Dango API - Documentation"
    )


# ==============================================================================
# Metabase Reverse Proxy with SSO
# ==============================================================================

import httpx
from typing import Dict, Any

# Store Metabase session for SSO
_metabase_session: Dict[str, Any] = {}


async def proxy_to_metabase(request: Request, target_path: str, session_id: str = None) -> Response:
    """
    Helper function to proxy a request to Metabase

    Args:
        request: The incoming FastAPI request
        target_path: The path to proxy to on Metabase (e.g., "/api/health")
        session_id: Optional Metabase session ID for auth

    Returns:
        Response from Metabase
    """
    metabase_url = "http://localhost:3000"
    target_url = f"{metabase_url}{target_path}"

    if request.url.query:
        target_url += f"?{request.url.query}"

    logger.debug(f"Proxying to Metabase: {request.method} {target_url}")

    # Prepare headers
    headers = {}
    for key, value in request.headers.items():
        if key.lower() not in ['host', 'connection', 'content-length']:
            headers[key] = value

    # Add session cookie if provided
    if session_id:
        existing_cookies = headers.get('cookie', '')
        if existing_cookies:
            headers['cookie'] = f"{existing_cookies}; metabase.SESSION={session_id}"
        else:
            headers['cookie'] = f"metabase.SESSION={session_id}"

    # Get request body if present
    body = None
    if request.method in ["POST", "PUT", "PATCH"]:
        body = await request.body()

    # Make proxy request
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
            proxy_response = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body
            )

            # Build response headers
            response_headers = {}
            for key, value in proxy_response.headers.items():
                if key.lower() not in ['content-encoding', 'transfer-encoding', 'content-length']:
                    response_headers[key] = value

            return Response(
                content=proxy_response.content,
                status_code=proxy_response.status_code,
                headers=response_headers
            )

    except Exception as e:
        logger.error(f"Metabase proxy error for {target_path}: {e}")
        return Response(
            content=f"Proxy error: {str(e)}",
            status_code=502
        )

# ==============================================================================
# Metabase-specific proxy routes (registered before Dango's /api routes)
# ==============================================================================

@app.api_route("/api/health", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_api_health(request: Request):
    """Proxy Metabase health check API"""
    session_id = await get_metabase_session()
    return await proxy_to_metabase(request, "/api/health", session_id)


@app.api_route("/api/session", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_api_session(request: Request):
    """Proxy Metabase session API"""
    return await proxy_to_metabase(request, "/api/session")


@app.api_route("/api/user", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_api_user(request: Request):
    """Proxy Metabase user API"""
    session_id = await get_metabase_session()
    return await proxy_to_metabase(request, "/api/user", session_id)


@app.api_route("/api/database", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@app.api_route("/api/database/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_api_database(request: Request, path: str = ""):
    """Proxy Metabase database API"""
    session_id = await get_metabase_session()
    target_path = f"/api/database/{path}" if path else "/api/database"
    return await proxy_to_metabase(request, target_path, session_id)


@app.api_route("/app/{path:path}", methods=["GET"])
async def metabase_app_assets(request: Request, path: str):
    """Proxy Metabase app assets (JS, CSS, images, etc.)"""
    return await proxy_to_metabase(request, f"/app/{path}")


@app.api_route("/public/{path:path}", methods=["GET"])
async def metabase_public_assets(request: Request, path: str):
    """Proxy Metabase public assets"""
    return await proxy_to_metabase(request, f"/public/{path}")


@app.get("/styles.css")
async def metabase_styles(request: Request):
    """Proxy Metabase styles.css"""
    return await proxy_to_metabase(request, "/styles.css")


async def get_metabase_session() -> str:
    """
    Get or create Metabase session for auto-login

    Returns:
        Session ID for Metabase
    """
    import yaml

    # Return cached session if valid
    if _metabase_session.get('id'):
        # TODO: Check if session is still valid
        return _metabase_session['id']

    # Load credentials
    try:
        project_root = get_project_root()
        metabase_config_file = project_root / ".dango" / "metabase.yml"

        if not metabase_config_file.exists():
            logger.error("Metabase config not found")
            return None

        with open(metabase_config_file, 'r', encoding='utf-8') as f:
            metabase_config = yaml.safe_load(f)

        admin_email = metabase_config.get('admin', {}).get('email')
        admin_password = metabase_config.get('admin', {}).get('password')

        if not admin_email or not admin_password:
            logger.error("Metabase credentials not found in config")
            return None

    except Exception as e:
        logger.error(f"Failed to load Metabase config: {e}")
        return None

    # Create new session by logging in
    try:
        async with httpx.AsyncClient() as client:
            login_response = await client.post(
                "http://localhost:3000/api/session",
                json={"username": admin_email, "password": admin_password},
                timeout=10.0
            )

            if login_response.status_code == 200:
                session_data = login_response.json()
                session_id = session_data.get("id")

                # Cache the session
                _metabase_session['id'] = session_id
                _metabase_session['email'] = admin_email

                logger.info(f"Created Metabase session: {session_id[:8]}...")
                return session_id
            else:
                logger.error(f"Metabase login failed: {login_response.status_code}")
                return None

    except Exception as e:
        logger.error(f"Error creating Metabase session: {e}")
        return None


@app.api_route("/metabase/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@app.api_route("/metabase", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def metabase_proxy(request: Request, path: str = ""):
    """
    Reverse proxy for Metabase with automatic SSO

    Routes all requests to http://localhost:3000 and automatically
    handles authentication by injecting session cookies.
    """
    metabase_url = "http://localhost:3000"

    # Build target URL
    if path:
        target_url = f"{metabase_url}/{path}"
    else:
        target_url = metabase_url

    if request.url.query:
        target_url += f"?{request.url.query}"

    logger.info(f"Proxying: {request.method} {target_url}")

    # Get or create Metabase session
    session_id = await get_metabase_session()

    # Prepare headers
    headers = {}
    for key, value in request.headers.items():
        # Skip headers that should not be forwarded
        if key.lower() not in ['host', 'connection', 'content-length']:
            headers[key] = value

    # Add session cookie if we have one
    if session_id:
        # Add to Cookie header
        existing_cookies = headers.get('cookie', '')
        if existing_cookies:
            headers['cookie'] = f"{existing_cookies}; metabase.SESSION={session_id}"
        else:
            headers['cookie'] = f"metabase.SESSION={session_id}"

    # Get request body if present
    body = None
    if request.method in ["POST", "PUT", "PATCH"]:
        body = await request.body()

    # Make proxy request
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
            proxy_response = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body
            )

            # Build response
            response_headers = {}
            for key, value in proxy_response.headers.items():
                # Skip headers that cause issues
                if key.lower() not in ['content-encoding', 'transfer-encoding', 'content-length']:
                    response_headers[key] = value

            # Return proxy response without modification (no nav bar injection)
            content = proxy_response.content
            return Response(
                content=content,
                status_code=proxy_response.status_code,
                headers=response_headers
            )

    except Exception as e:
        logger.error(f"Proxy error: {e}")
        return Response(
            content=f"Proxy error: {str(e)}",
            status_code=502
        )


# ==============================================================================
# dbt Docs Reverse Proxy
# ==============================================================================

# Proxy dbt docs assets that are loaded via absolute paths from JavaScript
@app.get("/manifest.json")
@app.get("/catalog.json")
async def dbt_docs_assets(request: Request):
    """
    Proxy dbt docs JSON assets

    dbt docs JavaScript loads these files using absolute paths,
    so we need to proxy them from the nginx container.
    """
    dbt_docs_url = "http://localhost:8081"
    target_url = f"{dbt_docs_url}{request.url.path}"

    if request.url.query:
        target_url += f"?{request.url.query}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            proxy_response = await client.get(target_url)

            # Return the JSON response
            return Response(
                content=proxy_response.content,
                status_code=proxy_response.status_code,
                headers=dict(proxy_response.headers)
            )
    except Exception as e:
        logger.error(f"dbt docs asset proxy error: {e}")
        return Response(
            content=f"Asset not found: {str(e)}",
            status_code=404
        )


@app.api_route("/dbt-docs/{path:path}", methods=["GET"])
@app.api_route("/dbt-docs", methods=["GET"])
async def dbt_docs_proxy(request: Request, path: str = ""):
    """
    Reverse proxy for dbt docs with nav bar injection

    Routes all requests to http://localhost:8081 and automatically
    injects Dango nav bar into HTML responses.
    """
    dbt_docs_url = "http://localhost:8081"

    # Build target URL
    if path:
        target_url = f"{dbt_docs_url}/{path}"
    else:
        target_url = dbt_docs_url

    if request.url.query:
        target_url += f"?{request.url.query}"

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            # Proxy the request
            proxy_response = await client.get(
                target_url,
                headers={k: v for k, v in request.headers.items()
                        if k.lower() not in ['host', 'connection']},
            )

            # Build response headers
            response_headers = {}
            for key, value in proxy_response.headers.items():
                # Skip headers that cause issues
                if key.lower() not in ['content-encoding', 'transfer-encoding', 'content-length']:
                    response_headers[key] = value

            # Return proxy response without modification (no nav bar injection)
            content = proxy_response.content
            return Response(
                content=content,
                status_code=proxy_response.status_code,
                headers=response_headers
            )

    except Exception as e:
        logger.error(f"dbt docs proxy error: {e}")
        return Response(
            content=f"Proxy error: {str(e)}",
            status_code=502
        )


# Application startup/shutdown events
@app.on_event("startup")
async def startup_event():
    """Run on application startup"""
    logger.info("=" * 80)
    logger.info("🔴 DANGO WEB API VERSION: 2025-11-04-v3")
    logger.info("🔴 FEATURES: Non-blocking row counts (2s timeout), immediate modal close on actions")
    logger.info("=" * 80)
    logger.info("Dango Web API starting up...")
    logger.info(f"Project root: {get_project_root()}")


@app.on_event("shutdown")
async def shutdown_event():
    """Run on application shutdown"""
    logger.info("Dango Web API shutting down...")


if __name__ == "__main__":
    import uvicorn

    # Run server for local development
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
        log_level="info"
    )
