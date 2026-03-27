# cli/

## Purpose

Click-based command-line interface for all Dango operations — project init, source management, sync, platform lifecycle, dbt transforms, Metabase management, and OAuth authentication.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `main.py` (109 lines) | CLI entry point, registers all commands | `cli` (Click group), `main()` |
| `__init__.py` (12 lines) | Shared `console` (Rich Console) instance | `console` |
| **commands/** | | |
| `commands/__init__.py` (4 lines) | Package marker | — |
| `commands/project.py` (292 lines) | `init`, `rename`, `info` | `init()`, `rename()`, `info()` |
| `commands/source.py` (658 lines) | `source` group (`add`, `list`, `remove`) + `sync` | `source`, `sync()` |
| `commands/platform.py` (979 lines) | `start`, `stop`, `status` | `start()`, `stop()`, `status()` |
| `commands/auth.py` (538 lines) | `auth` group (12 subcommands: enable, disable, add-user, list-users, reset-password, deactivate-user, reactivate-user, delete-user, status, unlock, audit, recover) | `auth`, `auth_enable()`, `auth_add_user()`, `auth_status()`, etc. |
| `commands/cleanup.py` (322 lines) | `cleanup` command — remove old log archives, dbt artifacts, Python cache | `cleanup()` |
| `commands/oauth.py` (812 lines) | `oauth` group (10 subcommands) | `oauth`, `oauth_setup()`, `oauth_status()`, `oauth_check()`, etc. |
| `commands/transform.py` (326 lines) | `run`, `docs`, `generate` | `run()`, `docs()`, `generate()` |
| `commands/upgrade.py` (236 lines) | `upgrade` command — local Dango upgrade via pip + migrations | `upgrade()`, `get_latest_version_cached()` |
| `commands/data.py` (360 lines) | `db` group (`status`, `clean`) + `validate` | `db`, `validate()` |
| `commands/config_cmd.py` (179 lines) | `config` group (`validate`, `show`) | `config` |
| `commands/metabase_cmd.py` (431 lines) | `metabase` group (`save`, `load`, `refresh`) | `metabase` |
| `commands/model.py` (212 lines) | `model` group (`add`, `remove`) | `model` |
| `commands/dashboard.py` (125 lines) | `dashboard` group (`provision`) | `dashboard` |
| `commands/remote.py` (652 lines) | `remote` group → `push`, `rollback`, `firewall`, `domain` subgroups + management commands | `remote`, `remote_push()`, `remote_rollback()`, `firewall`, `domain` |
| `commands/migrate.py` | `migrate` group (`status`, `run`) | `migrate` |
| `commands/serve.py` (~152 lines) | `serve` production foreground server | `serve()` |
| `commands/deploy.py` (689 lines) | `deploy` group (wizard default, --byos, destroy) | `deploy`, `deploy_destroy()` |
| `commands/deploy_wizard.py` (839 lines) | Interactive wizard steps 1-9 + BYOS wizard + non-interactive | `run_wizard()`, `run_non_interactive()`, `WizardConfig`, `run_byos_wizard()`, `run_byos_non_interactive()`, `BYOSConfig` |
| `commands/deploy_provision.py` (704 lines) | Provisioning orchestration (DO + BYOS) + cleanup | `run_provisioning()`, `run_byos_setup()`, `ProvisionResult`, `BYOSResult`, `_ResourceTracker` |
| `commands/remote_env.py` | `remote env` subgroup (set, get, list, delete) | `env` (Click group) |
| `commands/remote_ops.py` | `remote upgrade`, `remote resize`, `remote migrate` | `remote_upgrade()`, `remote_resize()`, `remote_migrate()` |
| `commands/remote_backup.py` | `remote backup` subgroup (list, enable, disable, download, restore) | `backup_group` |
| `commands/remote_mgmt.py` | `remote status`, `remote logs`, `remote ssh`, `remote query` | `remote_status()`, `remote_logs()` |
| `commands/schedule.py` (499 lines) | `schedule` group (add, list, remove, status, enable, disable, webhook) | `schedule`, `schedule_add()`, `schedule_list()`, `schedule_status()`, `schedule_webhook()` |
| `commands/governance.py` (125 lines) | `governance` group (drift-report, pii-report) | `governance`, `drift_report()`, `pii_report()` |
| `commands/notebook.py` (179 lines) | `notebook` group (new, open) + `snapshot` top-level | `notebook`, `notebook_new()`, `notebook_open()`, `snapshot()` |
| `commands/analyze.py` (81 lines) | `analyze` top-level command | `analyze()` |
| `commands/web.py` (66 lines) | `web` dev server command | `web()` |
| **Wizards** | | |
| `init.py` (1125 lines) | Project initialization wizard | `ProjectInitializer` |
| `wizard.py` (296 lines) | Interactive setup wizards | `ProjectWizard` |
| `source_wizard.py` (2145 lines) | Source configuration wizard | `add_source()` |
| `model_wizard.py` (507 lines) | dbt model creation wizard | `add_model()` |
| **Helpers** | | |
| `utils.py` (129 lines) | Display helpers + project context | `require_project_context()` |
| `validate.py` (651 lines) | Project validation logic | `validate_project()` |
| `db_helpers.py` (129 lines) | Schema/table matching for db commands | `build_schema_table_mapping()`, `is_table_configured()` |
| `env_helpers.py` (319 lines) | `.env` file management | `create_env_template()`, `validate_env_file()`, `guide_env_setup()` |
| `oauth.py` (428 lines) | OAuth CLI flows | `authenticate_facebook()`, `authenticate_google()`, `check_token_expiry()` |
| `schema_manager.py` (335 lines) | dbt `schema.yml` auto-generation | `update_model_schemas()` |
| `helpers/__init__.py` (6 lines) | Package marker | — |
| `helpers/port_manager.py` (48 lines) | Port checking | `check_port_in_use()` |
| `helpers/process_manager.py` (336 lines) | FastAPI server process management | `start_fastapi_server()` |

## Architecture

### Command Hierarchy

```
dango (top-level group)
├── init, rename, info          ← commands/project.py
├── start, stop, status         ← commands/platform.py
├── serve                       ← commands/serve.py
├── upgrade                     ← commands/upgrade.py
├── cleanup                     ← commands/cleanup.py
├── sync                        ← commands/source.py
├── run, docs, generate         ← commands/transform.py
├── validate                    ← commands/data.py
├── web                         ← commands/web.py
├── source (group)              ← commands/source.py
│   ├── add, list, remove
├── config (group)              ← commands/config_cmd.py
│   ├── validate, show
├── db (group)                  ← commands/data.py
│   ├── status, clean
├── auth (group)                ← commands/auth.py
│   ├── enable, disable, add-user, list-users, reset-password,
│   │   deactivate-user, reactivate-user, delete-user, status,
│   │   unlock, audit, recover
├── oauth (group)               ← commands/oauth.py
│   ├── status, setup, check, list, remove, refresh,
│   │   facebook_ads, google_sheets, google_analytics, google_ads
├── model (group)               ← commands/model.py
│   ├── add, remove
├── dashboard (group)           ← commands/dashboard.py
│   ├── provision
├── migrate (group)             ← commands/migrate.py
│   ├── status, run
├── remote (group)              ← commands/remote.py
│   ├── push, rollback          ← commands/remote.py
│   ├── status, logs, ssh, query ← commands/remote_mgmt.py
│   ├── upgrade, resize, migrate ← commands/remote_ops.py
│   ├── env (subgroup)          ← commands/remote_env.py
│   │   ├── set, get, list, delete
│   ├── firewall (subgroup)     ← commands/remote.py
│   │   ├── list, allow-ip, allow-all
│   ├── domain (subgroup)       ← commands/remote.py
│   │   ├── set, remove
│   └── backup (subgroup)       ← commands/remote_backup.py
│       ├── list, enable, disable, download, restore
├── schedule (group)            ← commands/schedule.py
│   ├── add, list, remove, status, enable, disable, webhook
├── deploy (group)              ← commands/deploy.py
│   ├── (default)  interactive wizard (DO or BYOS) → deploy_wizard.py + deploy_provision.py
│   ├── --byos     deploy to existing server (any provider)
│   └── destroy    tear down cloud infrastructure (DO resources or BYOS config)
├── analyze                     ← commands/analyze.py
├── snapshot                    ← commands/notebook.py
├── governance (group)          ← commands/governance.py
│   ├── drift-report, pii-report
├── notebook (group)            ← commands/notebook.py
│   ├── new, open              (default invocation lists notebooks)
└── metabase (group)            ← commands/metabase_cmd.py
    ├── save, load, refresh
```

### Patterns

- **Lazy imports:** All command functions use `from dango.xxx import ...` inside the function body, not at module level. This prevents circular imports and speeds up CLI startup.
- **Shared console:** All command modules import `console` from `dango.cli` for Rich terminal output.
- **Project root via context:** `ctx.obj["project_root"]` is set by the top-level `cli` group in `main.py` (via `dango.config.helpers.find_project_root`).
- **Cross-file command registration:** `remote_mgmt.py`, `remote_ops.py`, `remote_env.py`, and `remote_backup.py` import `remote` from `remote.py` and register commands via `@remote.command()` / `@remote.group()`. Registration is triggered by bottom-of-file imports in `remote.py`.
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
