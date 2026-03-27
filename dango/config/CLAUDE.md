# config/

## Purpose

Loads, validates, and manages dango project configuration files (project.yml, sources.yml) and dlt credentials.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Public exports, `__all__` | Re-exports all public symbols |
| `models.py` | Pydantic models for config data | `DangoConfig`, `ProjectContext`, `SourcesConfig`, `DataSource`, `SourceType`, `DeduplicationStrategy`, `PlatformSettings`, `Stakeholder`, `CSVSourceConfig`, `LocalFilesSourceConfig`, `GoogleSheetsSourceConfig`, `StripeSourceConfig`, `CloudConfig` (incl. `provider`, `firewall_id`, `deploy_branch`) |
| `loader.py` | Load/save YAML config files | `ConfigLoader` |
| `helpers.py` | Config convenience functions | `find_project_root`, `get_config`, `load_config`, `save_config`, `check_unreferenced_custom_sources`, `format_unreferenced_sources_warning` |
| `schedules.py` | Schedule config models, validation, reload | `ScheduleConfig`, `SchedulesConfig`, `ScheduleType`, `ReloadResult`, `CRON_PRESETS`, `load_schedules_config`, `validate_schedules`, `reload_schedules`, `log_startup_checks` |
| `credentials.py` | Credential loading for dlt sources | `CredentialManager`, `init_dlt_directory` |
| `exceptions.py` | Re-export shim — classes live in `dango/exceptions.py` | `ConfigError`, `ConfigNotFoundError`, `ConfigValidationError`, `ProjectNotFoundError` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new config field | `models.py` | `pytest tests/unit/test_config_models.py` |
| Add a new source type | `models.py` (`SourceType` enum) | `pytest tests/unit/test_config_models.py` |
| Change config loading logic | `loader.py` | `pytest tests/unit/test_config_loader.py` |
| Add a new config error type | `exceptions.py` | `pytest tests/unit/test_config_loader.py` |
| Add/modify schedule config | `schedules.py` | `pytest tests/unit/test_schedules_config.py tests/unit/test_schedules_loading.py` |
| Add a new credential provider | `credentials.py` | Manual: create test project with `.dlt/secrets.toml` |

## Dependencies

**Imports from:**
- `pydantic` — model validation (`BaseModel`, `Field`, `validator`)
- `yaml` — YAML parsing in `loader.py`, `schedules.py`
- `toml` — TOML parsing in `credentials.py`
- `croniter` — cron expression validation and iteration in `schedules.py`

**Used by:**
- `dango/cli/` — loads config to drive CLI commands
- `dango/ingestion/` — reads `DataSource`, `SourceType`, `DeduplicationStrategy`, `CSVSourceConfig`
- `dango/transformation/` — reads `DataSource`, `SourceType`, `DeduplicationStrategy`
- `dango/oauth/` — uses `CredentialManager` for token storage
- `dango/web/` — serves config through API endpoints
- `dango/platform/` — reads config for Docker and watcher setup
- `dango/platform/cloud/` — reads CloudConfig for deployment operations

## Testing

- **Unit:** `pytest tests/unit/test_config_loader.py tests/unit/test_config_models.py tests/unit/test_schedules_config.py tests/unit/test_schedules_loading.py`
- **Integration:** `pytest tests/integration/test_config_loading.py`
- **Manual:** `dango config show` in a dango project directory

## Key Conventions

- **`validate_schedules()` mixed return type:** Returns a tuple of `(errors, warnings)` where both are `list[str]`. Callers filter by string content to distinguish severity — there's no structured severity field. Structured return type deferred to Phase 8.
- **Dual cron preset drift:** `config/schedules.py` and `cli/commands/schedule.py` both define human-readable cron preset maps. Must stay in sync — see [`cli/CLAUDE.md`](../cli/CLAUDE.md) key conventions.

## Don't Modify

| File | Reason |
|------|--------|
| `models.py` field names | Changing field names breaks existing project.yml/sources.yml files |
| `models.py` `SourceType` enum values | Enum values are stored in user config files and referenced across modules |
| `exceptions.py` re-export shim | Exception classes are caught by name in cli/, web/, and ingestion/. Adding/removing classes here must mirror `dango/exceptions.py`. |
