"""dango/web/models.py

Pydantic request/response DTOs for the web API.
"""

from typing import Any

from pydantic import BaseModel


class TableInfo(BaseModel):
    """Table information for multi-resource sources."""

    name: str
    row_count: int
    schema: str


class SourceStatus(BaseModel):
    """Source status information."""

    name: str
    type: str
    enabled: bool
    last_sync: str | None = None
    row_count: int | None = None
    status: str = "unknown"  # "synced", "syncing", "failed", "unknown"
    freshness: dict[str, Any] | None = None  # Data freshness information
    tables: list[TableInfo] | None = None  # Per-table breakdown for multi-resource sources


class ServiceHealth(BaseModel):
    """Service health check response."""

    status: str
    dango_version: str = "0.1.0"
    services: dict[str, str]
    uptime: str


class SyncRequest(BaseModel):
    """Sync request parameters."""

    full_refresh: bool = False
    start_date: str | None = None
    end_date: str | None = None


class SyncResponse(BaseModel):
    """Sync response."""

    success: bool
    message: str
    source_name: str
    started_at: str


class LogEntry(BaseModel):
    """Log entry."""

    timestamp: str
    level: str
    message: str


class WatcherStatus(BaseModel):
    """File watcher status information."""

    running: bool
    pid: int | None = None
    auto_sync_enabled: bool
    auto_dbt_enabled: bool
    debounce_seconds: int
    watch_patterns: list[str]
    watch_directories: list[str]
    log_file: str | None = None
