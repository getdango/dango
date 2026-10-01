# cli/

## Purpose

Click-based command-line interface for all Dango operations — project init, source management, sync, platform lifecycle, dbt transforms, Metabase management, and OAuth authentication.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `main.py` (125 lines) | CLI entry point, registers all commands | `cli` (Click group), `main()` |
| `__init__.py` (12 lines) | Shared `console` (Rich Console) instance | `console` |
| **commands/** | | |
| `commands/__init__.py` (4 lines) | Package marker | — |
| `commands/project.py` (307 lines) | `init`, `rename`, `info` | `init()`, `rename()`, `info()` |
| `commands/source.py` (~1160 lines) | `source` group (`add`, `list`, `remove`, `edit`, `inspect-state`) + `sync` | `source`, `sync()`, `source_inspect_state()` |
| `commands/platform.py` (1136 lines) | `start`, `stop`, `status` | `start()`, `stop()`, `status()` |
| `commands/auth.py` (666 lines) | `auth` group (13 subcommands: enable, disable, add-user, list-users, reset-password, deactivate-user, reactivate-user, delete-user, status, unlock, change-role, audit, recover) | `auth`, `auth_enable()`, `auth_add_user()`, `auth_change_role()`, `auth_status()`, etc. |
| `commands/cleanup.py` (388 lines) | `cleanup` command — remove old log archives, dbt artifacts, Python cache | `cleanup()` |
| `commands/doctor.py` (65 lines) | `doctor` command — check credential health for all configured sources | `doctor()` |
| `commands/docker_audit.py` (~470 lines) | `docker-audit` command (1.0.8-Q8) — machine-wide diagnostic/cleanup for `dango-*` Docker resources, grouped by real Compose project identity (`com.docker.compose.project.working_dir` label) and classified orphaned / needs-attention / live. 1.0.8-Q12 added `_get_volume_labels()`, which reads back the `com.dango.project_name`/`com.dango.project_id` labels `dango init` now stamps onto the `metabase-data` volume (`templates/docker-compose.yml.j2`), so a group's identity survives its container being removed — display-only, never changes classification. Deliberately separate from `dango doctor`, which is scoped to credential health only. | `docker_audit()`, `_build_groups()`, `_get_volume_labels()`, `_remove_group()` |
| `commands/oauth.py` (842 lines) | `oauth` group (10 subcommands) | `oauth`, `oauth_setup()`, `oauth_status()`, `oauth_check()`, etc. |
| `commands/transform.py` (343 lines) | `run`, `docs`, `generate` | `run()`, `docs()`, `generate()` |
| `commands/upgrade.py` (236 lines) | `upgrade` command — local Dango upgrade via pip + migrations | `upgrade()`, `get_latest_version_cached()` |
| `commands/data.py` (388 lines) | `db` group (`status`, `clean`) + `validate` | `db`, `validate()` |
| `commands/config_cmd.py` (240 lines) | `config` group (`validate`, `show`, `do-token`) | `config` |
| `commands/metabase_cmd.py` (412 lines) | `metabase` group (`save`, `load`, `refresh`) | `metabase` |
| `commands/model.py` (244 lines) | `model` group (`add`, `remove`) | `model` |
| `commands/seed.py` (135 lines) | `seed` group (`add`, `list`) — dbt seed CSV management | `seed` |
| `commands/dashboard.py` (215 lines) | `dashboard` group (`provision`) — materializes pipeline-health state via `dango.utils.pipeline_health` before provisioning (1.0.8-DASH-1) | `dashboard` |
| `commands/mcp_server.py` (~485 lines) | `mcp` group + `run` command; FastMCP stdio server with 7 read-only tools (list_sources, get_catalog, get_lineage, list_models, get_model_sql, query, get_sync_history). `query` masks PII-flagged columns by default (1.0.10-M10). `mcp_run()` calls `_check_version_compatibility()` (1.0.8-OPS-4) once at startup, stderr-only. The server `instructions` string is asserted by `tests/integration/test_mcp_stdio.py` (1.0.10-M9a1) | `mcp_group`, `mcp`, `mcp_run()` |
| `commands/mcp_docs.py` (1.0.10-M11, ~500 lines) | Documentation tools on the shared `mcp` instance: `get_table_schema` (moved from mcp_server.py; now returns table/column descriptions + data_tests from model docs or `sources_<source>.yml`), `get_model_docs`, `get_source_docs` (labels `.dango/sources.yml` vs dbt source descriptions), `update_source_table_docs` (merge-edits a raw table entry in `sources_<source>.yml`; the generator never overwrites that file), `docs_coverage` (same TODO patterns as `dango validate`), `generate_docs` (`dbt docs generate` under `DbtLock`, 30 s acquire). Model/column docs are written with `update_model`. No stdout writes | `get_table_schema()`, `get_model_docs()`, `get_source_docs()`, `update_source_table_docs()`, `docs_coverage()`, `generate_docs()` |
| `commands/mcp_governance.py` (~270 lines) | Governance tools registered onto the shared `mcp` FastMCP instance (get_schema_drift, accept_schema_drift, get_pii_findings, scan_pii, list_pii_overrides, set_pii_override, delete_pii_override) plus `_mcp_pii_mask_columns()`, which `query()` uses to mask PII-flagged columns by output name (opt out: `api.mcp_mask_pii: false`). `set_pii_override`/`delete_pii_override` can only tighten classification (pii), never unmask | `_mcp_pii_mask_columns()`, the 7 tools |
| `commands/mcp_operations.py` | Operate tools registered onto the shared `mcp` FastMCP instance (run_sync, run_transform, run_doctor). `run_sync` mirrors `dango sync` (1.0.10-M4). `run_transform` mirrors `dango run` (1.0.10-M6b): DbtLock, cloud Metabase stop/start, `finalize_dbt_build` (model status, schema.yml sync, Metabase), and returns ANSI-stripped `output` plus structured `results` (`results.failed` names failing models/tests) and `post_build`; any failing test reports `failed`. No stdout writes | `run_sync()`, `run_transform()`, `run_doctor()` |
| `commands/mcp_sources.py` (~330 lines) | Source tools registered onto the shared `mcp` FastMCP instance: `list_source_types`, `get_source_setup_schema`, `create_source` (validated via `setup_service`; `file_path` copies a local file into `data/uploads/<source>/`), `update_source`, `set_source_enabled`, `remove_source` (dry_run/force), `validate_source` (readiness via `setup_lifecycle.credential_requirements`). Never accepts secrets; returns `credentials_required` + `next_steps`. Replaces the bare-entry `add_source` and the old `mcp_mutations.py` (1.0.10-M3b); successful writes add `git_warning` | `list_source_types()`, `get_source_setup_schema()`, `create_source()`, `update_source()`, `set_source_enabled()`, `remove_source()`, `validate_source()` |
| `commands/mcp_models.py` (1.0.10-M6a) | Model tools on the M5 model service (create_model, update_model, validate_model, remove_model). Thin layer: `ModelServiceError.errors` returned verbatim as `{"error", "errors"}`; `remove_model` uses `lock_source="mcp"`, `lock_timeout=30` and refreshes Metabase after a table drop; writes add `git_warning`. No stdout writes | `create_model()`, `update_model()`, `validate_model()`, `remove_model()` |
| `commands/mcp_schedules.py` (1.0.10-M1) | Schedule lifecycle MCP tools (list_schedules, add_schedule, update_schedule, set_schedule_enabled, remove_schedule, reload_schedules). Every mutation attempts to reload THIS project's running scheduler (gated on `.dango/web.pid` identity, not just an open port) and reports `activation` = reloaded / server_not_running / reload_failed, plus `next_run`. No stdout writes (stdio JSON-RPC) | `list_schedules()`, `add_schedule()`, `update_schedule()`, `set_schedule_enabled()`, `remove_schedule()`, `reload_schedules()` |
| `commands/mcp_remote.py` (1.0.10-M8) | Remote MCP tools over SSH (remote_status, remote_logs, remote_history, remote_query, remote_sync, remote_push). Reuses `remote_mgmt._make_ssh_manager` (pinned known_hosts) and the CLI's command strings; query/PII-names run as `sudo -u dango` (WAL ownership). Query is PII-masked (local ∪ server names, fail closed); logs redacted + capped; push defaults to dry-run, real push needs `confirm=True`, guardrails enforced, never forced | the 6 tools |
| `commands/mcp_setup.py` (~460 lines) | `mcp setup` / `mcp status` / `mcp remove` subcommands (registers onto `mcp_group` from mcp_server.py) (1.0.8-OPS-4). Claude Code goes through the client's own `claude mcp add/get/remove --scope local` CLI (subprocess), not a hand-written file; Cursor writes a project-scoped, git-committable `.cursor/mcp.json` (bare `dango` + `${workspaceFolder}` substitution); Windsurf keeps the global hand-written-file fallback (`_write_mcp_config()`/`_remove_mcp_config()`/`_atomic_write_json()`) with `DANGO_PROJECT_ROOT` injected, since it has no project-scoped config at all | `mcp_setup()`, `mcp_status()`, `mcp_remove()`, `_resolve_dango_cmd()`, `_write_mcp_config()`, `_remove_mcp_config()` |
| `commands/mcp_helpers.py` (~225 lines) | Plain-function helpers for the MCP read tools (project root, SELECT-only SQL validation, retry-on-lock DuckDB connect, blocking query execution) plus `_git_warnings()` (1.0.8-OPS-3) shared by the source/schedule/model tools — split out of mcp_server.py, not a Click module. `_get_project_root()` prefers the `DANGO_PROJECT_ROOT` env var over cwd-walking (1.0.8-OPS-4, set by `dango mcp setup`'s config entries); `_check_version_compatibility()` (1.0.8-OPS-4) is a one-time startup check in `mcp_run()`, not part of `_get_project_root()` itself | `_get_project_root()`, `_check_version_compatibility()`, `_validate_select_only()`, `_connect_readonly_with_retry()`, `_execute_query_sync()`, `_git_warnings()` |
| `commands/remote.py` (702 lines) | `remote` group → `push`, `rollback`, `firewall`, `domain` subgroups + management commands | `remote`, `remote_push()`, `remote_rollback()`, `firewall`, `domain` |
| `commands/migrate.py` | `migrate` group (`status`, `run`) | `migrate` |
| `commands/serve.py` (~205 lines) | `serve` production foreground server with `--workers` option. Metabase setup failure is non-fatal (prints warning, continues to uvicorn). | `serve()` |
| `commands/deploy.py` (767 lines) | `deploy` group (wizard default, --byos, destroy) | `deploy`, `deploy_destroy()` |
| `commands/deploy_wizard.py` (923 lines) | Interactive wizard steps 1-8 + BYOS wizard + non-interactive | `run_wizard()`, `run_non_interactive()`, `WizardConfig`, `run_byos_wizard()`, `run_byos_non_interactive()`, `BYOSConfig` |
| `commands/deploy_provision.py` (1000 lines) | Provisioning orchestration (DO + BYOS) + cleanup | `run_provisioning()`, `run_byos_setup()`, `ProvisionResult`, `BYOSResult`, `_ResourceTracker` |
| `commands/remote_env.py` | `remote env` subgroup (set, get, list, delete) | `env` (Click group) |
| `commands/remote_ops.py` | `remote upgrade`, `remote resize`, `remote migrate` | `remote_upgrade()`, `remote_resize()`, `remote_migrate()` |
| `commands/remote_backup.py` | `remote backup` subgroup (list, enable, disable, download, restore) | `backup_group` |
| `commands/remote_auth.py` (~217 lines) | `remote auth` subgroup (add-user, list-users, remove-user, reset-password) | `auth_group` |
| `commands/remote_mgmt.py` | `remote status`, `remote logs`, `remote ssh`, `remote query`, `remote history` | `remote_status()`, `remote_logs()` |
| `commands/remote_sync.py` (144 lines) | `remote sync` — trigger data syncs on cloud server via SSH; without `--wait` runs `cd <root> && nohup sudo -u dango ... &` (nohup on `sudo`, never the `cd` builtin) and exits 1 only if the SSH exec itself fails (the whole `&&` list is backgrounded, so remote startup failures such as a bad path or sudo error are not detectable from the exit code) | `remote_sync()` |
| `commands/schedule.py` (972 lines) | `schedule` group (add, list, remove, status, enable, disable, webhook). Supports SYNC, SYNC_ONLY, DBT, and SCRIPT types. | `schedule`, `schedule_add()`, `schedule_list()`, `schedule_status()`, `schedule_webhook()` |
| `commands/schedule_webhook.py` (226 lines) | `schedule webhook` subgroup (add, list, remove, test) | `webhook_group` |
| `commands/governance.py` (220 lines) | `governance` group (accept, drift-report, pii-report, pii-set, pii-list) | `governance`, `accept()`, `drift_report()`, `pii_report()`, `pii_set()`, `pii_list()` |
| `commands/notebook.py` (~209 lines) | `notebook` group (new, open) | `notebook`, `notebook_new()`, `notebook_open()` |
| `commands/snapshot.py` (383 lines) | `snapshot` group (add, list, run, db) | `snapshot`, `snapshot_add()`, `snapshot_list()`, `snapshot_run()`, `snapshot_db()` |
| `commands/analyze.py` (~97 lines) | `monitor` group + `analyze` alias | `monitor` (group), `monitor_run()`, `analyze()` |
| `commands/dev.py` (~429 lines) | `dev` group (default run + clean) — branch-based dbt development | `dev` (group), `dev_clean()` |
| `commands/web.py` (69 lines) | `web` dev server command | `web()` |
| `commands/telemetry.py` (~250 lines) | `telemetry` group (status, on, off) — unified write-through control for Dango, dbt, dlt, and Metabase telemetry | `telemetry`, `telemetry_status()`, `telemetry_on()`, `telemetry_off()` |
| **Wizards** | | |
| `init.py` (1585 lines) | Project initialization wizard, incl. first-run telemetry consent prompt | `ProjectInitializer` |
| `wizard.py` (307 lines) | Interactive setup wizards | `ProjectWizard` |
| `source_wizard.py` (2413 lines) | Source configuration wizard. `run()` calls `_print_git_warnings()` (1.0.8-OPS-3) right after the intro panel | `add_source()` |
| `model_wizard.py` (510 lines) | dbt model creation wizard (template, collision check and `dbt parse` delegate to `transformation/model_service.py`). `run()` calls `_print_git_warnings()` (1.0.8-OPS-3) right after the intro banner | `add_model()` |
| **Helpers** | | |
| `utils.py` (164 lines) | Display helpers + project context | `require_project_context()` |
| `validate.py` (787 lines) | Project validation logic | `validate_project()` |
| `db_helpers.py` (11 lines) | Re-exports from `utils/db_health.py` for backwards compatibility | `build_schema_table_mapping()`, `is_table_configured()` |
| `env_helpers.py` (319 lines) | `.env` file management | `create_env_template()`, `validate_env_file()`, `guide_env_setup()` |
| `oauth.py` (439 lines) | OAuth CLI flows | `authenticate_facebook()`, `authenticate_google()`, `check_token_expiry()` |
| `schema_manager.py` (352 lines) | dbt `schema.yml` auto-generation | `update_model_schemas()` |
| `helpers/__init__.py` (6 lines) | Package marker | — |
| `helpers/port_manager.py` (49 lines) | Port checking | `check_port_in_use()` |
| `helpers/process_manager.py` (345 lines) | FastAPI server process management | `start_fastapi_server()` |

## Architecture

### Command Hierarchy

```
dango (top-level group)
├── init, rename, info          ← commands/project.py
├── start, stop, status         ← commands/platform.py
├── serve                       ← commands/serve.py
├── upgrade                     ← commands/upgrade.py
├── cleanup                     ← commands/cleanup.py
├── doctor                      ← commands/doctor.py
├── docker-audit                ← commands/docker_audit.py
├── sync                        ← commands/source.py
├── run, docs, generate         ← commands/transform.py
├── validate                    ← commands/data.py
├── web                         ← commands/web.py
├── source (group)              ← commands/source.py
│   ├── add, list, remove, edit, inspect-state
├── config (group)              ← commands/config_cmd.py
│   ├── validate, show, do-token
├── db (group)                  ← commands/data.py
│   ├── status, clean
├── auth (group)                ← commands/auth.py
│   ├── enable, disable, add-user, list-users, reset-password,
│   │   deactivate-user, reactivate-user, delete-user, status,
│   │   unlock, change-role, audit, recover
├── oauth (group)               ← commands/oauth.py
│   ├── status, setup, check, list, remove, refresh,
│   │   facebook_ads, google_sheets, google_analytics, google_ads
├── model (group)               ← commands/model.py
│   ├── add, remove
├── seed (group)                ← commands/seed.py
│   ├── add, list
├── dashboard (group)           ← commands/dashboard.py
│   ├── provision
├── mcp (group)                 ← commands/mcp_server.py
│   ├── run                     ← commands/mcp_server.py (FastMCP stdio server; read tools in mcp_server.py,
│   │                              source tools in commands/mcp_sources.py, docs tools in commands/mcp_docs.py, helpers in commands/mcp_helpers.py)
│   └── setup, status, remove   ← commands/mcp_setup.py
├── migrate (group)             ← commands/migrate.py
│   ├── status, run
├── remote (group)              ← commands/remote.py
│   ├── push, rollback          ← commands/remote.py
│   ├── status, logs, ssh, query, history ← commands/remote_mgmt.py
│   ├── sync                     ← commands/remote_sync.py
│   ├── upgrade, resize, migrate ← commands/remote_ops.py
│   ├── repair, reset-metabase   ← commands/remote_repair.py (repair's remote schema scan resolves admin credentials through `dango.security.metabase_config`, with cloud mode explicit because SSH does not inherit the systemd environment)
│   ├── env (subgroup)          ← commands/remote_env.py
│   │   ├── set, get, list, delete
│   ├── auth (subgroup)         ← commands/remote_auth.py
│   │   ├── add-user, list-users, remove-user, reset-password
│   ├── firewall (subgroup)     ← commands/remote.py
│   │   ├── list, allow-ip, allow-all
│   ├── domain (subgroup)       ← commands/remote.py
│   │   ├── set, remove
│   └── backup (subgroup)       ← commands/remote_backup.py
│       ├── list, enable, disable, download, restore
├── schedule (group)            ← commands/schedule.py
│   ├── add, list, remove, status, enable, disable
│   └── webhook (subgroup)      ← commands/schedule_webhook.py
│       ├── add, list, remove, test
├── deploy (group)              ← commands/deploy.py
│   ├── (default)  interactive wizard (DO or BYOS) → deploy_wizard.py + deploy_provision.py
│   ├── --byos     deploy to existing server (any provider)
│   └── destroy    tear down cloud infrastructure (DO resources or BYOS config)
├── dev (group)                 ← commands/dev.py
│   ├── (default)  run dbt against copy of production DB
│   └── clean      remove dev artifacts (.dango/dev/)
├── analyze                     ← commands/analyze.py (alias for monitor run)
├── monitor (group)             ← commands/analyze.py
│   ├── run
├── snapshot (group)            ← commands/snapshot.py
│   ├── add, list, run, db
├── governance (group)          ← commands/governance.py
│   ├── accept, drift-report, pii-report, pii-set, pii-list
├── notebook (group)            ← commands/notebook.py
│   ├── new, open              (default invocation lists notebooks)
├── metabase (group)            ← commands/metabase_cmd.py
│   ├── save, load, refresh
└── telemetry (group)           ← commands/telemetry.py
    ├── status
    ├── on   [--all | --provider dango|dbt|dlt|metabase]
    └── off  [--all | --provider dango|dbt|dlt|metabase]
```

## Seeds vs CSV Data Source

| | dbt Seed | CSV Data Source |
|---|---|---|
| **Use when** | Static reference data (lookup tables, mappings, constituent lists) | Regularly updated data from external exports |
| **Added via** | `dango seed add` | `dango source add` → CSV type |
| **Lands in** | `seeds` schema | `raw_<source>` schema |
| **Refreshed by** | `dbt seed` (runs before dbt models) | `dango sync <source>` |
| **Version controlled** | Yes — CSV committed to `dbt/seeds/` | No — file dropped in `data/uploads/` |
| **Reference in models** | `{{ ref('seed_name') }}` directly in mart/intermediate | `{{ ref('stg_source__table') }}` via auto-generated staging |
| **Staging wrapper?** | Never — reference seed directly | Auto-generated by Dango |
| **Raw layer?** | None — seeds land directly in `seeds` schema | `raw_<source>` tables created by dlt |

**Rule of thumb:** If the data changes regularly and comes from an external system, use a CSV source. If it's reference data you maintain (e.g. a list of stock tickers, a country code table), use a seed.

### Patterns

- **Lazy imports:** All command functions use `from dango.xxx import ...` inside the function body, not at module level. This prevents circular imports and speeds up CLI startup.
- **Shared console:** All command modules import `console` from `dango.cli` for Rich terminal output.
- **Project root via context:** `ctx.obj["project_root"]` is set by the top-level `cli` group in `main.py` (via `dango.config.helpers.find_project_root`).
- **Cross-file command registration:** `remote_auth.py`, `remote_mgmt.py`, `remote_ops.py`, `remote_env.py`, `remote_backup.py`, `remote_repair.py`, and `remote_sync.py` import `remote` from `remote.py` and register commands via `@remote.command()` / `@remote.group()`. Registration is triggered by bottom-of-file imports in `remote.py`. Same pattern for `mcp`: `mcp_setup.py` imports `mcp_group` from `mcp_server.py` and registers `setup`/`status`/`remove` via `@mcp_group.command()`; `mcp_sources.py` imports `mcp` (the FastMCP instance, not the Click group) from `mcp_server.py` and registers its source tools via `@mcp.tool()`; both are triggered by bottom-of-file imports in `mcp_server.py`. `mcp_helpers.py` is a *different* kind of split — plain functions, no Click decorators, imported normally at the top of `mcp_server.py` (not bottom-of-file, no registration side effect involved). All three splits exist to stay under the 500-line file-size check.
- **MCP tool functions are plain functions, not wrapped:** `@mcp.tool()` (fastmcp) returns the original function unchanged — call `mcp_server.get_catalog()` / `mcp_docs.get_table_schema()` (read tools) or `mcp_operations.run_sync()` / `mcp_sources.create_source()` etc. (mutation tools) directly in tests, no `.fn` unwrapping needed. All `dango.*` imports inside MCP tool bodies must stay lazy (never at module top level) — the server communicates over stdio and any stray stdout write corrupts the JSON-RPC protocol. `list_sources` and `query` are `async def` tools (fastmcp supports both sync and async tools natively) — call them with `await` under `@pytest.mark.anyio`, matching the pattern in `test_rate_limit_proxy.py`. Prefer `async def` over a sync tool doing `asyncio.run()` internally: the latter only works because fastmcp currently dispatches sync tools to a worker thread by default (`run_in_thread=True`, undocumented in our code) — an `async def` tool awaits directly on fastmcp's own loop with no such dependency.
- **Two SSH users:** `root` for system ops (backup, rollback, domain, server setup) via `load_cloud_config_with_ssh()` in `cli/utils.py`. `dango` for project file ops (.env, .dlt/secrets.toml) via `_ssh_connect_or_fail()` in `remote.py`.

### Key conventions

- **Dual cron preset drift risk:** Both `config/schedules.py` and `cli/commands/schedule.py` define human-readable cron preset maps (e.g., "every 6 hours" → `0 */6 * * *`). These must stay in sync — changes to one without updating the other cause inconsistent behavior between CLI and config validation.

### Known pre-existing bugs

None currently tracked. The `lock.release()` bug in `source.py` and `transform.py` was fixed in P5-008 (added `lock._acquired` guard).

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new top-level command | Create in `commands/`, register in `main.py` via `cli.add_command()` | `pytest tests/unit/test_cli_commands.py` |
| Add subcommand to existing group | Add to relevant `commands/*.py` | `dango <group> --help` |
| Add a new command group | Create in `commands/`, register in `main.py` | `dango <group> --help` |
| Modify source wizard | `source_wizard.py` | `dango source add` |
| Add new project init step | `init.py` (`ProjectInitializer`) | `dango init` in temp dir |
| Modify validation logic | `validate.py` | `dango validate` |

## Dependencies

**Imports from:**
- `click` — command framework
- `rich` — terminal output (`Console`, `Panel`, `Table`)
- `inquirer` — interactive prompts (wizards)
- `dango.config/` — `ConfigLoader`, models, helpers (most commands)
- `dango.ingestion/` — `run_sync`, source registry (source commands)
- `dango.oauth/` — `OAuthManager`, providers, storage (auth commands)
- `dango.transformation/` — `DbtModelGenerator` (transform commands)
- `dango.visualization/` — `DashboardManager`, metabase functions (dashboard/metabase commands)
- `dango.platform/` — `DockerManager`, `NetworkConfig`, watcher lifecycle (platform commands)
- `dango.utils/` — `DbtLock`, process utilities, `dbt_status` (various commands)
- `dango.web/` — app instance (web dev command)

**Used by:**
- `pyproject.toml` entry point: `dango = "dango.cli.main:cli"`
- No other dango modules import from `cli/` (Level 3 = top of hierarchy)

## Testing

- **Unit:** `pytest tests/unit/test_cli_commands.py`
- **Manual:** `dango --help`, `dango <command> --help`, run commands in a test project

## Don't Modify

| File | Reason |
|------|--------|
| `main.py` command registration order | Groups and commands are registered in logical order for `--help` output |
| Click context pattern (`ctx.obj`) | All commands depend on `project_root` from context |
| Lazy import pattern in commands | Prevents circular imports and speeds up CLI startup |
