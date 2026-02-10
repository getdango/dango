# transformation/

## Purpose

Integrates with dbt for data transformation, including auto-generation of staging models from ingested data sources with deduplication strategy mapping.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | dbt CLI execution functions | `run_dbt_models`, `generate_dbt_docs`, `_get_dbt_executable` |
| `generator.py` | Auto-generates dbt staging models from data sources | `DbtModelGenerator`, `generate_staging_models` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Change dbt run behavior | `__init__.py` (`run_dbt_models`) | Manual: `dango transform` |
| Add a new dedup strategy mapping | `generator.py` (`CSV_TO_DBT_STRATEGY_MAP`) | Manual: `dango generate-models` with source using new strategy |
| Change generated model SQL | `generator.py` + `dango/templates/dbt/staging_model.sql.j2` | Manual: `dango generate-models` and inspect output |
| Change sources.yml generation | `generator.py` (`generate_sources_yml`) | Manual: `dango generate-models` and inspect `dbt/models/staging/` |

## Dependencies

**Imports from:**
- `dango/config/models.py` — `DataSource`, `SourceType`, `DeduplicationStrategy`
- `dango/utils/dbt_status.py` — `update_model_status` (lazy import in `run_dbt_models`)

**Used by:**
- `dango/cli/main.py` — `DbtModelGenerator` for `dango generate-models` command
- `dango/ingestion/dlt_runner.py` — `DbtModelGenerator`, `run_dbt_models`, `generate_dbt_docs` for post-sync auto-transform
- `dango/web/app.py` — `run_dbt_models` via API endpoint
- `dango/platform/watcher_runner.py` — `generate_dbt_docs` for watch-triggered doc generation

## Testing

- **Unit:** None yet (will be `tests/unit/test_transformation.py`)
- **Integration:** None yet (will be `tests/integration/test_transformation.py`)
- **Manual:** `dango generate-models` then `dango transform` in a dango project directory

## Don't Modify

| File | Reason |
|------|--------|
| `generator.py` dedup strategy mappings (`CSV_TO_DBT_STRATEGY_MAP`, `DBT_TEMPLATE_STRATEGIES`) | Maps config dedup strategies to dbt SQL patterns; changes must be coordinated with `config/models.py` `DeduplicationStrategy` enum |
| Jinja2 templates in `dango/templates/dbt/` | Templates define generated model SQL syntax; changes cascade to all auto-generated models |
