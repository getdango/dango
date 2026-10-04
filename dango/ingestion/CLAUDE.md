# ingestion/

## Purpose

Loads data into DuckDB from external sources via dlt pipelines and local files, with a central registry of 34 supported data sources.

## Source Selection

Registry contains 34 sources: 27 dlt verified (vendored in `dlt_sources/`) + CSV
(custom, hidden) + Local Files (unified, primary wizard entry) + dlt_native
(passthrough) + filesystem (hidden, cloud storage) + rest_api (dlt core built-in) +
PostgreSQL + MySQL (dlt sql_database wrapper). Excluded: generic `sql_database` (too
complex for wizard — use dlt_native) and Shopify (`wizard_enabled=False`, see P5-006).

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Public exports | `DltPipelineRunner`, `run_sync`, `CSVLoader`, `SOURCE_REGISTRY`, `CATEGORIES`, `get_source_metadata`, `get_source_capabilities` |
| `dlt_runner.py` | Generic pipeline runner for all dlt and custom sources | `DltPipelineRunner`, `run_sync` (accepts `progress_callback` for dbt phase reporting; imports `SyncTimeoutError` from `dango.exceptions`); `_apply_dlt_telemetry_env()` — sets dlt's `RUNTIME__DLTHUB_TELEMETRY` env var from the machine-level opt-out (1.0.8-OPS-2), called at the top of both `_run_dlt_native_source()` and `_run_dlt_source()`, before any dlt config resolution |
| `csv_loader.py` | Multi-format file loading (CSV, JSON, JSONL, Parquet) with metadata tracking and 4 dedup strategies | `CSVLoader`, `SUPPORTED_READ_FUNCTIONS` (imports `CSVSchemaMismatchError` from `dango.exceptions`) |
| `credential_health.py` | Cross-references configured sources against available credentials (OAuth tokens, API keys, service accounts) | `run_credential_checks()`, `get_cached_credential_health(refresh=)` (5-minute in-process cache; `refresh=True` bypasses; API-key checks use each source's own env var; never creates `.dlt/secrets.toml`) |
| `sources/__init__.py` | Sources subpackage exports | Re-exports `SOURCE_REGISTRY`, `CATEGORIES`, `get_source_metadata`, `get_source_capabilities` |
| `sources/registry.py` | Central registry of 34 supported data sources with metadata | `SOURCE_REGISTRY`, `CATEGORIES`, `AuthType`, `get_source_metadata`, `get_sources_by_category`, `get_source_capabilities` |
| `sources/setup_schema.py` | Source-setup types, setup schema, and parameter normalisation (no I/O) — the single implementation behind the wizard and MCP | `SourceSetupError`, `CredentialRequirement`, `SourceSetupResult`, `REPLACE_MODE_SOURCE_TYPES`, `is_credential_param()`, `compute_env_var_name()`, `get_setup_schema()`, `normalize_params()` |
| `sources/setup_service.py` | Non-interactive source setup: validate (`prepare_source`, no writes) then persist (`apply_source`) — default_config, geo targets, file directories, `.env`/secrets.toml templates, `sources.yml`; wizard delegates to these | `create_source()`, `prepare_source()`, `apply_source()`, `build_source_config()`, `write_default_config()`, `provision_geo_targets()`, `prepare_source_directory()`, `write_secrets_toml_template()`, `append_source()` (saves sources.yml only, never project.yml), `add_analysis_monitors()` |
| `sources/setup_lifecycle.py` | Non-interactive source lifecycle with the same validation as create: `update_source` (existing values untouched unless supplied), `credential_requirements()` (public, read-only, uncached: unset env vars + OAuth for a configured source; a stored `*_env` value that is not an uppercase env var name is reported by param name, never echoed; `redact_env_fields()` for MCP output), `set_source_enabled`, `remove_source` (dry_run/force; downstream-model detection, shared `[sources.<type>]` kept in `.dlt/config.toml` while another source of the type exists, analysis monitors removed, `.env` names reported not values); `dango source remove` delegates | `update_source()`, `set_source_enabled()`, `remove_source()`, `SourceRemovalResult` |
| `dlt_sources/` | dlt verified source implementations (27 directories, 105+ files) — helper files are custom Dango code | See "Don't Modify" section for guidelines |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new source to the registry | `sources/registry.py` (`SOURCE_REGISTRY` dict) | Manual: `dango add` and check new source appears |
| Change CSV loading behavior | `csv_loader.py` | `pytest tests/unit/test_csv_loader.py` (when created) |
| Change sync execution logic | `dlt_runner.py` | Manual: `dango sync <source_name>` |
| Add a new dedup strategy | `csv_loader.py` + `config/models.py` (`DeduplicationStrategy`) | Manual: sync CSV source with new strategy |
| Change dlt telemetry opt-out env injection | `dlt_runner.py` (`_apply_dlt_telemetry_env`) — mirror both call sites (`_run_dlt_native_source`, `_run_dlt_source`) | `pytest tests/unit/test_telemetry_unified.py tests/unit/test_egress_allowlist.py` |

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
| `dlt_sources/` structure | Don't add/remove source directories; but helper files (e.g. `helpers/data_processing.py`) are custom Dango implementations — safe to modify |
| `sources/registry.py` source metadata structure | Registry keys (`dlt_source_name`, `dlt_function`, `auth_type`, etc.) are referenced by `dlt_runner.py` and `cli/source_wizard.py` |
