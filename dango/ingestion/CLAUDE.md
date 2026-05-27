# ingestion/

## Purpose

Loads data into DuckDB from external sources via dlt pipelines and local files, with a central registry of 33 supported data sources.

## Source Selection

Registry contains 33 sources: 27 dlt verified (vendored in `dlt_sources/`) + CSV
(custom, hidden) + Local Files (unified, primary wizard entry) + dlt_native
(passthrough) + filesystem (hidden, cloud storage) + rest_api (dlt core built-in) +
PostgreSQL (dlt sql_database wrapper). Excluded: generic `sql_database` (too complex
for wizard — use dlt_native) and Shopify (`wizard_enabled=False`, see P5-006).

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Public exports | `DltPipelineRunner`, `run_sync`, `CSVLoader`, `SOURCE_REGISTRY`, `CATEGORIES`, `get_source_metadata`, `get_source_capabilities` |
| `dlt_runner.py` | Generic pipeline runner for all dlt and custom sources | `DltPipelineRunner`, `run_sync` (accepts `progress_callback` for dbt phase reporting; imports `SyncTimeoutError` from `dango.exceptions`) |
| `csv_loader.py` | Multi-format file loading (CSV, JSON, JSONL, Parquet) with metadata tracking and 4 dedup strategies | `CSVLoader`, `SUPPORTED_READ_FUNCTIONS` (imports `CSVSchemaMismatchError` from `dango.exceptions`) |
| `sources/__init__.py` | Sources subpackage exports | Re-exports `SOURCE_REGISTRY`, `CATEGORIES`, `get_source_metadata`, `get_source_capabilities` |
| `sources/registry.py` | Central registry of 33 supported data sources with metadata | `SOURCE_REGISTRY`, `CATEGORIES`, `AuthType`, `get_source_metadata`, `get_sources_by_category`, `get_source_capabilities` |
| `dlt_sources/` | Third-party dlt verified source implementations (27 directories, 105+ files) | DO NOT MODIFY — see "Don't Modify" section |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new source to the registry | `sources/registry.py` (`SOURCE_REGISTRY` dict) | Manual: `dango add` and check new source appears |
| Change CSV loading behavior | `csv_loader.py` | `pytest tests/unit/test_csv_loader.py` (when created) |
| Change sync execution logic | `dlt_runner.py` | Manual: `dango sync <source_name>` |
| Add a new dedup strategy | `csv_loader.py` + `config/models.py` (`DeduplicationStrategy`) | Manual: sync CSV source with new strategy |

## Dependencies

**Imports from:**
- `dango/config/models.py` — `DataSource`, `SourceType`, `DeduplicationStrategy`, `CSVSourceConfig`, `RESTAPISourceConfig`, `DltNativeConfig`
- `dango/oauth/storage.py` — `OAuthStorage` for token expiry checks (lazy import in `dlt_runner.py`)
- `dango/utils/` — `activity_log`, `sync_history`, `db_health` (lazy imports in `dlt_runner.py`)
- `dango/transformation/` — `DbtModelGenerator`, `run_dbt_models`, `generate_dbt_docs` (lazy imports for post-sync auto-transform)
- `dango/visualization/metabase.py` — `refresh_metabase_connection`, `sync_metabase_schema` (lazy imports for post-sync Metabase refresh)

**Used by:**
- `dango/cli/main.py` — `run_sync` for `dango sync` command
- `dango/cli/source_wizard.py` — registry queries during `dango add`
- `dango/cli/validate.py` — `get_source_metadata`, `AuthType` for source validation
- `dango/web/app.py` — `run_sync` for API-triggered syncs
- `dango/transformation/generator.py` — `get_source_metadata` for dbt model generation

## Testing

- **Unit:** None yet (will be `tests/unit/test_ingestion.py`)
- **Integration:** None yet (will be `tests/integration/test_ingestion.py`)
- **Manual:** `dango sync <source_name>` in a dango project directory

## Source Registry Conventions

### `incremental` capability flag

The `incremental` flag in `sources/registry.py` means the source uses incremental loading **by default**, not just that it supports it. Sources with mixed `write_disposition` (some resources incremental, some full refresh) should be marked based on their predominant default behavior. When adding a new source, verify the actual `write_disposition` in the `dlt_sources/` code — do not assume from documentation alone.

## Don't Modify

| File | Reason |
|------|--------|
| `dlt_sources/` (entire directory) | Third-party dlt verified source implementations; updates come from upstream dlt, local changes would be lost |
| `sources/registry.py` source metadata structure | Registry keys (`dlt_source_name`, `dlt_function`, `auth_type`, etc.) are referenced by `dlt_runner.py` and `cli/source_wizard.py` |
