# visualization/

## Purpose

Integrates with Metabase for dashboard provisioning, auto-setup, schema synchronization, and git-based dashboard export/import.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Public exports | `provision_dashboard`, `create_pipeline_health_dashboard` |
| `metabase.py` | Metabase auto-setup, DuckDB connection, schema sync | `MetabaseProvisioner`, `setup_metabase`, `sync_metabase_schema`, `refresh_metabase_connection` |
| `dashboard_manager.py` | YAML-based dashboard/question export and import with rollback | `DashboardManager`, `import_dashboards` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Change Metabase auto-setup behavior | `metabase.py` (`setup_metabase`) | Manual: `dango start` on fresh project |
| Add a new pipeline health query | `metabase.py` (`DASHBOARD_QUERIES` dict) | Manual: `dango metabase provision` |
| Change dashboard export format | `dashboard_manager.py` (`card_to_yaml`, `dashboard_to_yaml`) | Manual: `dango metabase export` |
| Change schema visibility rules | `metabase.py` (`sync_metabase_schema`) | Manual: `dango metabase sync` |

## Dependencies

**Imports from:**
- No dango module imports (isolated module). Uses `requests` to call Metabase API, reads credentials from `.dango/metabase.yml` at runtime.

**Used by:**
- `dango/cli/main.py` — Metabase CLI commands (`dango metabase setup/sync/export/import/provision/refresh`)
- `dango/ingestion/dlt_runner.py` — `sync_metabase_schema`, `refresh_metabase_connection` after data syncs
- `dango/web/app.py` — `sync_metabase_schema`, `refresh_metabase_connection` via API endpoints

## Testing

- **Unit:** None yet (will be `tests/unit/test_visualization.py`)
- **Integration:** None yet (will be `tests/integration/test_visualization.py`)
- **Manual:** `dango start` then `dango metabase export` / `dango metabase import`

## Don't Modify

| File | Reason |
|------|--------|
| `metabase.py` Metabase API response handling | Tied to Metabase REST API format; changes must match Metabase version expectations |
| `metabase.py` `DASHBOARD_QUERIES` SQL | Pre-defined queries reference internal dango table schemas (`_dango_*`, `_dlt_*`); changes must match warehouse schema |
| `dashboard_manager.py` YAML serialization format | Exported YAML files may be committed to user git repos; format changes break existing exports |
