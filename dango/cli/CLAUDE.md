# cli/

## Purpose

Click-based command-line interface for all Dango operations — project init, source management, sync, platform lifecycle, dbt transforms, Metabase management, and OAuth authentication.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `main.py` (86 lines) | CLI entry point, registers all commands | `cli` (Click group), `main()` |
| `__init__.py` (12 lines) | Shared `console` (Rich Console) instance | `console` |
| **commands/** | | |
| `commands/__init__.py` (4 lines) | Package marker | — |
| `commands/project.py` (292 lines) | `init`, `rename`, `info` | `init()`, `rename()`, `info()` |
| `commands/source.py` (521 lines) | `source` group (`add`, `list`, `remove`) + `sync` | `source`, `sync()` |
| `commands/platform.py` (955 lines) | `start`, `stop`, `status` | `start()`, `stop()`, `status()` |
| `commands/auth.py` (707 lines) | `auth` group (10 subcommands) | `auth`, `auth_setup()`, `auth_status()`, `auth_check()`, etc. |
| `commands/transform.py` (326 lines) | `run`, `docs`, `generate` | `run()`, `docs()`, `generate()` |
| `commands/data.py` (360 lines) | `db` group (`status`, `clean`) + `validate` | `db`, `validate()` |
| `commands/config_cmd.py` (179 lines) | `config` group (`validate`, `show`) | `config` |
| `commands/metabase_cmd.py` (431 lines) | `metabase` group (`save`, `load`, `refresh`) | `metabase` |
| `commands/model.py` (212 lines) | `model` group (`add`, `remove`) | `model` |
| `commands/dashboard.py` (125 lines) | `dashboard` group (`provision`) | `dashboard` |
| `commands/web.py` (66 lines) | `web` dev server command | `web()` |
| **Wizards** | | |
| `init.py` (965 lines) | Project initialization wizard | `ProjectInitializer` |
| `wizard.py` (296 lines) | Interactive setup wizards | `ProjectWizard` |
| `source_wizard.py` (1324 lines) | Source configuration wizard | `add_source()` |
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
│   ├── status, setup, check, list, remove, refresh,
│   │   facebook_ads, google_sheets, google_analytics, google_ads
├── model (group)               ← commands/model.py
│   ├── add, remove
├── dashboard (group)           ← commands/dashboard.py
│   ├── provision
└── metabase (group)            ← commands/metabase_cmd.py
    ├── save, load, refresh
```

### Patterns

- **Lazy imports:** All command functions use `from dango.xxx import ...` inside the function body, not at module level. This prevents circular imports and speeds up CLI startup.
- **Shared console:** All command modules import `console` from `dango.cli` for Rich terminal output.
- **Project root via context:** `ctx.obj["project_root"]` is set by the top-level `cli` group in `main.py` (via `dango.config.helpers.find_project_root`).

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
