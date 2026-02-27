# web/

## Purpose

FastAPI web server providing REST API and WebSocket for managing Dango data pipelines. Serves the dashboard UI via Jinja2 templates, proxies Metabase and dbt docs, and exposes endpoints for source sync, CSV upload, platform health, user authentication, and admin user management.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `app.py` | Entry point: `create_app()`, middleware, router registration, lifecycle events (incl. first-run admin creation), global exception handlers (`DangoError` → structured JSON, generic `Exception` → 500) | `create_app()`, `app` (global FastAPI instance), `startup_event()`, `dango_error_handler()`, `unhandled_error_handler()` |
| `models.py` | Pydantic request/response DTOs | `TableInfo`, `SourceStatus`, `ServiceHealth`, `SyncRequest`, `SyncResponse`, `LogEntry`, `WatcherStatus`, `LoginRequest`, `AcceptInviteRequest`, `TwoFAVerifyRequest` |
| `helpers.py` | Shared helpers: DuckDB queries, config loading, service health, logging | `get_project_root()`, `load_sources_config()`, `get_duckdb_path()`, `get_dbt_models()`, `mask_sensitive_config()`, `get_source_freshness()`, `append_log_entry()`, `load_all_logs()`, `check_service_status_async()`, `get_platform_health_data()`, `get_source_status_data()` |
| `__init__.py` | Public exports | `app` module |
| `middleware/auth.py` | Session/API key auth + CSRF check on every request (~325 lines) | `AuthMiddleware`, `is_secure_request()`, `COOKIE_NAME` |
| `middleware/rate_limit.py` | Rate limiting (login 10/min, API 200/min, localhost exempt, ~212 lines) | `RateLimitMiddleware` |
| `templates/base.html` | Shared Jinja2 layout: head, header, nav bar, footer, script blocks | Blocks: `title`, `subtitle_attrs`, `header_right`, `content`, `footer`, `scripts` |
| `templates/dashboard.html` | Dashboard page (extends `base.html`) — service cards, tabs, modals | Loads `app.js` |
| `templates/health.html` | Health page (extends `base.html`) — platform metrics, issues | Inline JS for health polling |
| `templates/logs.html` | Logs page (extends `base.html`) — filterable log table | Loads `logs.js` |
| `templates/login.html` | Login page — Alpine.js two-step state machine (`credentials` → `totp`) | Lockout display, recovery code toggle |
| `templates/change_password.html` | First-login password change (forced by `must_change_password` flag) | — |
| `templates/admin_users.html` | Admin user management page — invite, edit roles, deactivate | Alpine.js with modal dialogs |
| `templates/account.html` | User account settings — change password, sessions, API keys, 2FA | Alpine.js `accountPage()` component |
| `templates/invite.html` | Invite acceptance page — set password for invited users | — |
| `templates/secrets.html` | Secrets management page (env vars + OAuth credentials) | — |
| `routes/__init__.py` | Package marker | — |
| `routes/auth.py` | Login/logout, password change, OAuth flows, invite accept, API key CRUD (~854 lines) | `_bridge_metabase_session()`, `_set_session_cookie()` |
| `routes/auth_2fa.py` | TOTP 2FA setup/verify/disable/recovery (~328 lines) | — |
| `routes/users.py` | Admin user CRUD: create, edit, deactivate, delete, unlock, invite (525 lines) | — |
| `routes/health.py` | `/api/status`, `/api/watcher/status`, `/api/health/platform` | — |
| `routes/config.py` | `/api/config`, `/api/metabase-config` | — |
| `routes/sources.py` | `/api/sources`, `/api/sources/{name}/details` | — |
| `routes/sync.py` | `/api/sources/{name}/sync`, `/api/sync/trigger` (remote), `/api/sync/status/{id}` + background `run_sync_task()` (~558 lines) | `run_sync_task()` |
| `routes/logs.py` | `/api/logs`, `/api/sources/{name}/logs` | — |
| `routes/dbt.py` | `/api/dbt/models`, `/api/dbt/models/{name}/run` + dbt docs proxy (`/manifest.json`, `/catalog.json`, `/dbt-docs/*`) | `run_dbt_model_task()` |
| `routes/upload.py` | CSV upload/list/delete + background `run_dbt_after_delete()` | `run_dbt_after_delete()` |
| `routes/websocket.py` | `ConnectionManager`, `ws_manager` singleton, `/ws` endpoint | `ConnectionManager`, `ws_manager` |
| `routes/ui.py` | `/`, `/health`, `/logs`, `/api`, `/api/docs`, `/api/redoc`, `/login`, `/account`, `/admin/users`, `/invite/{token}` | `templates`, `_render_template()` |
| `routes/metabase_proxy.py` | All Metabase proxy routes + SSO session state | `proxy_to_metabase()`, `get_metabase_session()` |
| `routes/secrets.py` | Secrets and OAuth credential management (admin-only, .env + .dlt/secrets.toml CRUD) | `router` |
| `routes/oauth_connect.py` | Web-based OAuth connect/callback for cloud deployments | `router` |
| `routes/schedules.py` | Schedule CRUD, trigger, reload, cancel, history, notification config/test, `/schedules` page (~720 lines) | `router` |
| `routes/initial_sync.py` | Initial data sync after first deploy (deploy token auth) | `router` |
| `templates/schedules.html` | Schedule management page (extends `base.html`) — table, modals, WebSocket | Alpine.js `schedulesPage()` component |
| `static/` | CSS and JS assets (`css/main.css`, `js/app.js`, `js/logs.js`) | — |

## Architecture

```
                    ┌─────────────────┐
                    │     app.py      │
                    │  create_app()   │
                    └────────┬────────┘
                             │ registers
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
    ┌──────────────┐  ┌────────────┐  ┌──────────────┐
    │  middleware/  │  │  routes/*  │  │  templates/  │
    │  auth.py     │  │  (routers) │  │  (Jinja2)    │
    │  rate_limit  │  └─────┬──────┘  └──────────────┘
    └──────────────┘        │ imports
                    ┌───────┼───────┐
                    ▼       ▼       ▼
              helpers.py models.py dango.auth/
```

**Middleware layer:** Every request passes through `middleware/auth.py` (session/API key validation, CSRF check) and `middleware/rate_limit.py` before reaching route handlers. The middleware sets `request.state.user` and `request.state.auth_method` for downstream use.

**Frontend approach (ADR-007):** Alpine.js for new page interactivity + Jinja2 base templates for shared layout. Existing pages use vanilla JS. See `docs/decisions/ADR-007-frontend-approach.md`.

**`login.html` state machine:** The login page uses an Alpine.js two-step state machine. Step 1 (`credentials`): email + password form. On success with `requires_2fa`, transitions to Step 2 (`totp`): TOTP code entry with recovery code toggle. Lockout state displays remaining time. All API calls include `X-Requested-With` header for CSRF.

**Router registration order matters:** Dango API routers are registered first in `app.py`, then proxy routers. Catch-all routes like `/metabase/{path:path}` must be last to avoid shadowing Dango API endpoints.

**Circular import prevention:** `helpers.py` uses a lazy import (`from dango.web.app import app`) inside `get_project_root()` to avoid circular imports at module load time. This is safe because `app = create_app()` executes before any router imports.

## Key Conventions

- **`{{ data | tojson }}` never `{{ json_string | safe }}`** in Jinja2 templates. `| safe` disables HTML escaping and creates XSS vulnerabilities. `| tojson` safely serializes Python objects to JSON with proper escaping.
- **Health endpoint integration checklist:** When adding status data to `/api/health/platform`: (1) add data to the result dict, (2) contribute to the warnings list so it affects overall status, (3) place the warning append before the `if critical_issues:` / `elif warnings:` block. Missing step 2 means silent failures.
- **FastAPI route ordering:** Literal routes (e.g., `/api/schedules/history/recent`) must be registered BEFORE parameterized routes (e.g., `/api/schedules/{name}/history`), or FastAPI captures the literal segment as the path parameter.
- **Manual body parsing needs try/except:** `await request.json()` + `Model(**body)` bypasses FastAPI's built-in validation handler. Wrap in try/except to return proper 422 responses.
- **Every admin endpoint must call `log_auth_event()`** from `dango.auth.audit` — this is a mandatory checklist item for audit compliance.
- **BackgroundTasks run synchronously in TestClient:** Starlette's `TestClient` executes `background_tasks.add_task(fn, ...)` before returning the response. Mock the background function at the route module level (e.g., `@patch("dango.web.routes.sync._run_manual_sync")`).
- **TOCTOU in validate-then-background-execute:** Endpoints that validate inputs then launch background tasks must re-validate inside the background function. State can change between validation and execution — add explicit guards.
- **Rename-via-PUT guard:** Any CRUD API where the resource name is in both URL path and request body must check `body.name != url_name → 400`. Without this, renames silently orphan related records (e.g., execution history keyed by schedule name).
- **`load_sources_config()` patching:** `load_sources_config()` in `helpers.py` internally calls `get_project_root()`. Patching `get_project_root` at the route module level doesn't reach the call inside `load_sources_config()`. Patch `load_sources_config` directly at the route module level instead.
- **Alpine.js object reactivity:** Proxy doesn't track `delete obj[key]` or direct property mutation. Must use object spread reassignment (`this.obj = {...updated}`) for reactive updates. Arrays with `.push()`/`.splice()` work fine; objects do not.
- **Dual logger debt in `routes/sync.py`:** Uses both stdlib `logging` and structlog `get_logger`. Known debt — consolidate when touching the file.

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
- `dango.auth/` — session validation, permission checking, audit logging, user models, Metabase bridge
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
- **Auth unit tests:** `pytest tests/unit/test_web_auth*.py tests/unit/test_web_users*.py tests/unit/test_auth_middleware*.py`
- **Integration:** `pytest tests/integration/` — full auth flows with real database
- **Manual:** `dango web dev` in a dango project directory, then browse `http://localhost:8800`

## Don't Modify

| File/Path | Reason |
|-----------|--------|
| `static/` | CSS/JS assets; changes here require frontend knowledge |
| Metabase proxy paths in `routes/metabase_proxy.py` | Metabase client-side code depends on these exact paths |
| dbt docs proxy paths in `routes/dbt.py` | dbt docs JavaScript loads assets via absolute paths |
| Router registration order in `app.py` | Proxy catch-all routes must be registered last |
| `middleware/auth.py` CSRF check logic | Security-critical; changes need security review |
