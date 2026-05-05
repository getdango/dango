"""dango/web/routes/sources.py

Source listing and detail endpoints.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from dango.validation import validate_source_name
from dango.web.helpers import (
    get_source_freshness,
    get_source_row_count,
    get_source_status_data,
    get_source_tables_info,
    load_sources_config,
    load_sync_history,
    mask_sensitive_config,
)
from dango.web.models import SourceStatus, TableInfo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sources"])

# Cron expression → human-readable display string.
# Keep in sync with CRON_PRESETS in config/schedules.py and
# _FREQUENCY_CHOICES in cli/commands/schedule.py.
_CRON_DISPLAY: dict[str, str] = {
    "0 */1 * * *": "Every hour",
    "0 * * * *": "Every hour",  # CRON_PRESETS "every_hour" variant
    "0 */2 * * *": "Every 2 hours",
    "0 */4 * * *": "Every 4 hours",
    "0 */6 * * *": "Every 6 hours",
    "0 */12 * * *": "Every 12 hours",
    "*/15 * * * *": "Every 15 minutes",  # CRON_PRESETS "every_15m"
    "0 0 * * *": "Daily at midnight",
    "0 6 * * *": "Daily at 6 AM",
    "0 0 * * 0": "Weekly (Sunday)",
    "0 0 * * 1": "Weekly (Monday)",
    "0 6 * * 1": "Weekly (Monday at 6 AM)",  # CRON_PRESETS "weekly"
}


def _cron_to_display(cron: str) -> str:
    """Convert a cron expression to a human-readable string."""
    return _CRON_DISPLAY.get(cron, cron)


def _build_schedule_map() -> dict[str, str]:
    """Build source_name → schedule display mapping."""
    try:
        from dango.config.schedules import load_schedules_config
        from dango.web.helpers import get_project_root

        config = load_schedules_config(get_project_root())
        mapping: dict[str, str] = {}
        for schedule in config.schedules:
            if not schedule.enabled:
                continue
            display = _cron_to_display(schedule.cron)
            for src in schedule.sources:
                mapping[src] = display
        return mapping
    except Exception:
        return {}


@router.get("/api/sources", response_model=list[SourceStatus])
async def get_sources() -> list[SourceStatus]:
    """List all configured data sources with status.

    Returns:
        List of sources with sync status, row counts, and timestamps
    """
    # Load sources config in thread pool
    sources_config = await asyncio.to_thread(load_sources_config)

    # Process all sources concurrently
    tasks = [get_source_status_data(source) for source in sources_config]
    source_statuses = await asyncio.gather(*tasks)

    # Enrich with schedule info
    schedule_map = await asyncio.to_thread(_build_schedule_map)
    for status in source_statuses:
        sched = schedule_map.get(status.name)
        if sched:
            status.has_schedule = True
            status.schedule_display = sched

    # Enrich with attention state (breaking drift)
    try:
        from dango.governance.schema_drift import get_sources_needing_attention
        from dango.web.helpers import get_project_root

        attention_rows = await asyncio.to_thread(get_sources_needing_attention, get_project_root())
        attention_map = {r["source"]: r["reason"] for r in attention_rows}
        for status in source_statuses:
            if status.name in attention_map:
                status.needs_attention = True
                status.attention_reason = attention_map[status.name]
    except Exception:
        pass  # Non-critical enrichment

    # Sort sources alphabetically by name for consistent ordering
    source_statuses = sorted(source_statuses, key=lambda s: s.name.lower())

    return source_statuses


@router.get("/api/sources/{source_name}/details")
async def get_source_details(source_name: str) -> dict[str, object]:
    """Get detailed information about a specific source.

    Args:
        source_name: Name of the source

    Returns:
        Source configuration (masked) and sync history
    """
    source_name = validate_source_name(source_name)
    sources_config = await asyncio.to_thread(load_sources_config)

    # Find the source
    source_config = None
    for source in sources_config:
        if source.get("name") == source_name:
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
    if tables_info and tables_info.get("has_multiple_tables"):
        tables = [TableInfo(**t) for t in tables_info["tables"]]

    # Derive sync mode and lookback
    from dango.ingestion.sources.registry import get_source_capabilities, get_source_metadata

    capabilities = get_source_capabilities(source_config.get("type", ""))
    supports_incremental = capabilities.get("incremental", True) if capabilities else True
    sync_mode = "incremental" if supports_incremental else "full_refresh"

    lookback_days = source_config.get("lookback_days")
    if lookback_days is None:
        meta = get_source_metadata(source_config.get("type", ""))
        if meta:
            lookback_days = (meta.get("default_config") or {}).get("lookback_days")

    return {
        "name": source_name,
        "config": masked_config,
        "history": history,
        "row_count": row_count,
        "freshness": freshness,
        "tables": tables,
        "sync_mode": sync_mode,
        "lookback_days": lookback_days,
    }
