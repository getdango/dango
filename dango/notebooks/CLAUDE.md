# notebooks/

## Purpose

Marimo notebook server lifecycle, DuckDB read-only snapshots, file-level locking for concurrent editing protection, and HTTP/WebSocket reverse proxy utilities. CLI commands for creating, listing, and opening notebooks.

## Files

| File | Lines | Purpose | Key Exports |
|------|-------|---------|-------------|
| `__init__.py` | ~45 | Re-exports public symbols | All public API |
| `manager.py` | ~262 | Marimo process lifecycle (PID file, start/stop/status) + idle auto-shutdown | `get_marimo_pid_file_path()`, `start_marimo()`, `stop_marimo()`, `get_marimo_status()`, `start_idle_checker()`, `stop_idle_checker()` |
| `snapshot.py` | ~143 | DuckDB snapshot management | `create_snapshot()`, `list_snapshots()`, `cleanup_snapshots()` |
| `locking.py` | ~285 | File-level notebook locking via `notebook_locks` table | `acquire_lock()`, `release_lock()`, `refresh_lock()`, `force_release_lock()`, `expire_stale_locks()`, `is_locked()`, `get_lock_info()`, `copy_locked_notebook()` |
| `proxy.py` | ~186 | HTTP + WebSocket reverse proxy to Marimo | `proxy_to_marimo()`, `proxy_websocket_to_marimo()` |
| `templates/__init__.py` | ~5 | Package marker | — |
| `templates/explore.py` | ~30 | Data exploration starter template | `app` (marimo.App) |
| `templates/quality.py` | ~40 | Data quality starter template | `app` (marimo.App) |
| `templates/blank.py` | ~20 | Minimal blank starter template | `app` (marimo.App) |

## Architecture

```
CLI (notebook new/open/list, snapshot)
  │
  ├─ manager.py       Marimo server lifecycle (PID file at .dango/marimo.pid)
  ├─ snapshot.py       DuckDB snapshots (.dango/snapshots/)
  ├─ locking.py        Lock management (notebook_locks table in .dango/dango.db)
  └─ proxy.py          HTTP + WS proxy (used by web routes in P7-008)
```

### Key Design Decisions

- **Marimo runs headless** (`--headless --no-token`) — Dango handles auth via its own middleware.
- **Snapshots use `shutil.copy2()`** — simple file copy of `data/warehouse.duckdb`. DuckDB single-writer constraint means notebooks need read-only copies.
- **Locks are time-limited** (15 minutes) — expired locks are garbage-collected automatically on every lock operation.
- **Proxy is bidirectional** — HTTP proxy for REST, WebSocket proxy for Marimo's reactive kernel communication.

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Change Marimo server flags | `manager.py` (`start_marimo()` subprocess args) | `pytest tests/unit/test_notebook_manager.py` |
| Change lock duration | `locking.py` (`_LOCK_DURATION_MINUTES`, SQL `'+15 minutes'`) | `pytest tests/unit/test_notebook_locking.py` |
| Add a new template | `templates/{name}.py` + update CLI `--template` choices in `cli/commands/notebook.py` | `dango notebook new --template <name> --name test` |
| Change snapshot retention | `snapshot.py` (`cleanup_snapshots()` `keep` parameter) | `pytest tests/unit/test_notebook_snapshot.py` |
| Modify proxy headers | `proxy.py` (`_HOP_BY_HOP` frozenset) | `pytest tests/unit/test_notebook_proxy.py` |

## Dependencies

**Imports from:**
- `dango.utils.process` — `is_process_running()`, `kill_process()` (manager.py)
- `dango.utils.dango_db` — `connect()` context manager (locking.py)
- `dango.config.loader` — `ConfigLoader` (manager.py: reads `marimo_port` from config)
- `dango.auth.audit` — `AuditEvent`, `log_auth_event()` (used by CLI commands)
- `httpx` — HTTP proxy transport (proxy.py)
- `websockets` — WebSocket proxy transport (proxy.py)
- `marimo` — Referenced in templates (not imported by module code)

**Used by:**
- `dango/cli/commands/notebook.py` — `notebook` CLI group + `snapshot` command
- `dango/cli/commands/platform.py` — Marimo stop in `dango stop`
- `dango/web/routes/` — proxy utilities wired by P7-008

## Database Tables

Located in `.dango/dango.db` (managed by `dango/utils/dango_db.py`):

- **`notebook_locks`** — `notebook_id` (PK), `locked_by`, `locked_at`, `expires_at`, `last_heartbeat_at` (added via `_ADDITIVE_DDL`)
- **`notebook_metadata`** — `id` (PK), `name`, `description`, `created_by`, `created_at`, `updated_at`

## Testing

```
pytest tests/unit/test_notebook_manager.py tests/unit/test_notebook_snapshot.py \
  tests/unit/test_notebook_locking.py tests/unit/test_notebook_proxy.py \
  tests/unit/test_notebook_cli.py -v
```

## Don't Modify

| Item | Reason |
|------|--------|
| PID file path (`.dango/marimo.pid`) | `cli/commands/platform.py` reads this to stop Marimo |
| `notebook_locks` / `notebook_metadata` schema | Schema defined in `utils/dango_db.py`, shared across modules |
| Snapshot filename format (`warehouse_{user}_{ts}.duckdb`) | `_parse_snapshot_filename()` and `list_snapshots()` depend on this format |
