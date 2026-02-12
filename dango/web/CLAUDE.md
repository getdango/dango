# web/

## Purpose

FastAPI web server providing REST API and WebSocket for managing Dango data pipelines. Serves the dashboard UI via Jinja2 templates, proxies Metabase and dbt docs, and exposes endpoints for source sync, CSV upload, and platform health.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `app.py` | Entry point: `create_app()`, middleware, router registration, lifecycle events, global exception handlers (`DangoError` → structured JSON, generic `Exception` → 500) | `create_app()`, `app` (global FastAPI instance), `dango_error_handler()`, `unhandled_error_handler()` |
| `models.py` | Pydantic request/response DTOs | `TableInfo`, `SourceStatus`, `ServiceHealth`, `SyncRequest`, `SyncResponse`, `LogEntry`, `WatcherStatus` |
| `helpers.py` | Shared helpers: DuckDB queries, config loading, service health, logging | `get_project_root()`, `load_sources_config()`, `get_duckdb_path()`, `get_dbt_models()`, `mask_sensitive_config()`, `get_source_freshness()`, `append_log_entry()`, `load_all_logs()`, `check_service_status_async()`, `get_platform_health_data()`, `get_source_status_data()` |
| `__init__.py` | Public exports | `app` module |
| `templates/base.html` | Shared Jinja2 layout: head, header, nav bar, footer, script blocks | Blocks: `title`, `subtitle_attrs`, `header_right`, `content`, `footer`, `scripts` |
| `templates/dashboard.html` | Dashboard page (extends `base.html`) — service cards, tabs, modals | Loads `app.js` |
| `templates/health.html` | Health page (extends `base.html`) — platform metrics, issues | Inline JS for health polling |
| `templates/logs.html` | Logs page (extends `base.html`) — filterable log table | Loads `logs.js` |
| `routes/__init__.py` | Package marker | — |
| `routes/health.py` | `/api/status`, `/api/watcher/status`, `/api/health/platform` | — |
| `routes/config.py` | `/api/config`, `/api/metabase-config` | — |
| `routes/sources.py` | `/api/sources`, `/api/sources/{name}/details` | — |
| `routes/sync.py` | `/api/sources/{name}/sync` + background `run_sync_task()` | `run_sync_task()` |
| `routes/logs.py` | `/api/logs`, `/api/sources/{name}/logs` | — |
| `routes/dbt.py` | `/api/dbt/models`, `/api/dbt/models/{name}/run` + dbt docs proxy (`/manifest.json`, `/catalog.json`, `/dbt-docs/*`) | `run_dbt_model_task()` |
| `routes/upload.py` | CSV upload/list/delete + background `run_dbt_after_delete()` | `run_dbt_after_delete()` |
| `routes/websocket.py` | `ConnectionManager`, `ws_manager` singleton, `/ws` endpoint | `ConnectionManager`, `ws_manager` |
| `routes/ui.py` | `/`, `/health`, `/logs`, `/api`, `/api/docs`, `/api/redoc` | `templates`, `_render_template()` |
| `routes/metabase_proxy.py` | All Metabase proxy routes + SSO session state | `proxy_to_metabase()`, `get_metabase_session()` |
| `static/` | CSS and JS assets (`css/main.css`, `js/app.js`, `js/logs.js`) | — |

## Architecture

```
app.py ──imports──> routes/*.py (router objects)
routes/*.py ──imports──> helpers.py (helper functions)
routes/*.py ──imports──> models.py (Pydantic models)
routes/ui.py ──renders──> templates/*.html (Jinja2 templates)
routes/sync.py, upload.py, dbt.py ──imports──> routes/websocket.py (ws_manager)
helpers.py ──lazy import──> app.py (for app.state.project_root)
```

**Frontend approach (ADR-007):** Alpine.js for new page interactivity + Jinja2 base templates for shared layout. Existing pages use vanilla JS. See `docs/decisions/ADR-007-frontend-approach.md`.

**Router registration order matters:** Dango API routers are registered first in `app.py`, then proxy routers. Catch-all routes like `/metabase/{path:path}` must be last to avoid shadowing Dango API endpoints.

**Circular import prevention:** `helpers.py` uses a lazy import (`from dango.web.app import app`) inside `get_project_root()` to avoid circular imports at module load time. This is safe because `app = create_app()` executes before any router imports.

## Adding a New Page

1. **Create a template** in `templates/` extending `base.html`:
   ```html
   {% extends "base.html" %}

   {% block title %}My Page - Dango{% endblock %}

   {% block content %}
       <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
           <div x-data="myPage()" x-init="init()">
               <template x-for="item in items" :key="item.id">
                   <div x-text="item.name"></div>
               </template>
           </div>
       </main>
   {% endblock %}

   {% block scripts %}
       <script>
           function myPage() {
               return {
                   items: [],
                   async init() {
                       const response = await fetch('/api/my-endpoint');
                       this.items = await response.json();
                   }
               }
           }
       </script>
   {% endblock %}
   ```

2. **Add a route handler** in `routes/ui.py`:
   ```python
   @router.get("/my-page")
   async def my_page(request: Request):
       """Serve the my page UI."""
       return templates.TemplateResponse(
           "my_page.html",
           {
               "request": request,
               "version": dango.__version__,
               "current_page": "my-page",
               "subtitle": "My Page",
           },
       )
   ```

3. **Use Alpine.js** (`x-data`, `x-init`, `x-for`, `x-if`, `x-text`, `x-bind`) for reactive state. Keep API calls in the Alpine component or a separate JS file for complex pages.

4. **Template blocks available in `base.html`:**
   - `title` — page title in `<title>` tag
   - `subtitle_attrs` — HTML attributes for subtitle `<p>` tag (override to add `id` etc.)
   - `header_right` — content in header right side (e.g., connection status indicator)
   - `content` — main page body
   - `footer` — override to customize footer (default shows version + links)
   - `scripts` — page-specific JavaScript (loaded at end of `<body>`)

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new page | Create template in `templates/`, add route in `routes/ui.py` | Manual: `dango web dev` |
| Add a new API endpoint | Create/modify route file in `routes/`, register router in `app.py` | `pytest tests/unit/test_web_imports.py` |
| Add a new Metabase proxy path | `routes/metabase_proxy.py` | Manual: `dango web dev` |
| Add a new response model | `models.py` | `pytest tests/unit/test_web_imports.py` |
| Add a new helper function | `helpers.py` | Unit test for the new function |
| Modify WebSocket broadcast format | `routes/websocket.py` (`ConnectionManager.broadcast()`) | Manual: observe WebSocket messages in browser |
| Add new dbt docs proxy paths | `routes/dbt.py` (bottom section) | Manual: open `/dbt-docs` in browser |
| Change shared layout (header, nav, footer) | `templates/base.html` | Manual: check all pages |

## Dependencies

**Imports from:**
- `dango.config/` — `ConfigLoader`, `load_config` (for project config, platform settings)
- `dango.ingestion/` — `run_sync` (triggered by sync endpoint)
- `dango.transformation/` — `run_dbt_models` (triggered by upload delete)
- `dango.visualization/` — `sync_metabase_schema`, `refresh_metabase_connection` (after sync/dbt runs)
- `dango.utils/` — `DbtLock`, `DbtLockError`, `activity_log`, `sync_history`, `db_health`, `dbt_status`
- `dango.platform/` — `platform.watcher_lifecycle.get_watcher_status` (for watcher status endpoint)

**Used by:**
- `dango/cli/commands/web.py` — imports `from dango.web import app` to run uvicorn

## Testing

- **Unit:** `pytest tests/unit/test_web_imports.py` — verifies all modules import cleanly and routes are registered
- **Manual:** `dango web dev` in a dango project directory, then browse `http://localhost:8800`

## Don't Modify

| File/Path | Reason |
|-----------|--------|
| `static/` | CSS/JS assets; changes here require frontend knowledge |
| Metabase proxy paths in `routes/metabase_proxy.py` | Metabase client-side code depends on these exact paths |
| dbt docs proxy paths in `routes/dbt.py` | dbt docs JavaScript loads assets via absolute paths |
| Router registration order in `app.py` | Proxy catch-all routes must be registered last |
