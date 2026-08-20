# catalog/

## Purpose

Standalone data-access layer for the data catalog — column schema, profiling, lineage, and model/source browsing. No FastAPI dependency. Extracted from `web/routes/catalog.py` to enable reuse by CLI commands and future API consumers.

## Files

| File | Purpose | Key Functions |
|------|---------|---------------|
| `__init__.py` | Public API surface (5 aliases) | `get_lineage`, `get_impact`, `get_models`, `search_catalog`, `get_column_schema` |
| `schema.py` | DuckDB introspection (column schema, row counts, table discovery) | `_get_column_schema`, `_get_model_column_schema`, `_get_row_count`, `_source_schema_exists`, `_table_exists`, `_get_raw_tables_from_duckdb` |
| `profiling.py` | Cached profiling stats readers from SQLite | `_get_cached_stats`, `_get_profiled_at`, `_get_cached_row_counts` |
| `lineage.py` | dbt manifest DAG + impact analysis readers | `_build_lineage_dag`, `_get_impact_tree`, `_build_impact_response` |
| `manifest.py` | Pure manifest-node readers (no I/O) | `_build_test_status_map`, `_classify_model_type`, `_model_profiling_key`, `_find_model_in_manifest`, `_search_manifest` |
| `models.py` | Response builders (depends on manifest.py + profiling.py) | `_get_run_results`, `_get_source_summary_stats`, `_build_catalog_models`, `_build_model_detail` |

## Key Conventions

**Private function names (_underscore prefix):** All functions are private. Test code patches them by their names at their call sites:
- Functions called directly in route handlers: patch at `dango.web.routes.catalog._X` (e.g., `_build_lineage_dag` called via lazy import in route)
- Functions imported at module level in sub-modules: patch at `dango.catalog.<submodule>._X` (e.g., `_get_cached_row_counts` imported in `models.py`, patched at `dango.catalog.models._get_cached_row_counts`)

Route handlers import all functions with `from dango.catalog.X import _name` to preserve 176+ existing test @patch sites; the import location determines the patch target. Do not rename these.

**FastAPI coupling limitation:** Functions `_build_lineage_dag`, `_build_catalog_models`, and `_get_run_results` internally call `dango.web.helpers.get_project_root()` with no parameters. This reads `app.state.project_root`, populated only by the FastAPI `lifespan()` hook in `dango.web.app`. These functions still require a booted FastAPI app and will raise `RuntimeError` if called from a bare CLI script. Fixing this requires reordering statements inside route handlers (route-logic surgery, not extraction) — out of scope here. **Documented follow-up:** reorder `list_catalog_models` and `get_lineage` handlers to populate `project_root` before calling these functions, then update them to accept `project_root` as a parameter.

**`_model_profiling_key` lockstep:** This function's keying scheme must stay in exact sync with the write path in `dango.utils.post_sync.py` (specifically `profile_table` callers). `profiling_stats.source` is 3-way overloaded (source name, dbt schema, or "main" for seeds). Diff the body of `_model_profiling_key` before and after any edit to confirm zero drift.

## Dependencies

**Imports from:**
- `duckdb` — DuckDB introspection in `schema.py`
- `dango.utils.dango_db.connect` — SQLite context manager in `profiling.py`
- `dango.web.helpers.get_dbt_manifest` — manifest loading in `lineage.py` (⚠ FastAPI-coupled)
- `dango.logging.get_logger` — logging in `models.py`
- `dango.web.helpers.get_project_root` — project root lookup in `models.py` (⚠ FastAPI-coupled)
- `dango.utils.dbt_status.get_model_statuses` — model execution history in `models.py` (lazy import inside function body)

**Used by:**
- `dango.web.routes.catalog` — all private functions imported directly for `asyncio.to_thread` dispatch
- `dango.web.routes.ai` — may use `get_column_schema` (public alias) in future

**No imports from:**
- `fastapi` — guaranteed no FastAPI dependency in any module
- `dango.web.app` — would create circular import at module load time

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new DuckDB query helper | `schema.py` | `pytest tests/unit/test_catalog_schema.py` |
| Add a new profiling stats reader | `profiling.py` | `pytest tests/unit/test_catalog_profiling.py` |
| Add a new lineage or impact function | `lineage.py` | `pytest tests/unit/test_catalog_lineage.py` |
| Add a new manifest searcher | `manifest.py` | `pytest tests/unit/test_catalog_manifest.py` |
| Add a new response builder | `models.py` | `pytest tests/unit/test_catalog_models.py` |
| Verify no FastAPI import | Any `.py` file | `python -c "import dango.catalog.X; import fastapi"` (should fail if FastAPI leaked) |
| Change public API surface | `__init__.py` | Verify all 5 aliases resolve + update repo CLAUDE.md |

## Testing

- **All catalog tests:** `pytest tests/ -k "catalog" -v`
- **Unit only:** `pytest tests/unit/test_catalog*.py -v`
- **Integration:** `pytest tests/integration/test_catalog_integration.py -v`
- **No FastAPI:** `python -c "import dango.catalog.schema; import fastapi" 2>&1 | grep -q "No module" && echo "ok"` (expects ImportError)

## Don't Modify

| Function | Reason |
|----------|--------|
| `_model_profiling_key` | Keying scheme in lockstep with `dango.utils.post_sync.py` — audit before changes |
| All `_*` names (underscore prefix) | 176+ test @patch sites depend on these names resolving in `dango.web.routes.catalog` |
| `__init__.py` public surface | Established in task spec; only `get_lineage`, `get_impact`, `get_models`, `search_catalog`, `get_column_schema` are public |
| Function signatures | Tests and route handlers dispatch via `asyncio.to_thread` with exact parameter counts |
