# CLAUDE.md Template for Dango Modules

Use this template when creating a `CLAUDE.md` file for a dango module directory. All 6 sections are required. Copy the blank template below and fill it in for your module.

---

## Blank Template

```markdown
# {module_name}/

## Purpose

{One sentence describing what this module does and why it exists.}

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | {purpose} | {exports} |
| `{file}.py` | {purpose} | {key symbols} |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| {task description} | `{file}` | `pytest tests/unit/{test_file}` |

## Dependencies

**Imports from:**
- `{module}` — {what is imported and why}

**Used by:**
- `{module}` — {how this module is consumed}

## Testing

- **Unit:** `pytest tests/unit/test_{module}.py`
- **Integration:** `pytest tests/integration/test_{module}.py`
- **Manual:** {describe manual verification steps if any}

## Don't Modify

| File | Reason |
|------|--------|
| `{file}` | {why it should not be modified} |
```

---

## Filled Example: `config/`

Below is a filled-in example using the `dango/config/` module to demonstrate each section.

```markdown
# config/

## Purpose

Loads, validates, and manages dango project configuration files (project.yml, sources.yml).

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Public exports, `__all__` | Re-exports all public symbols |
| `loader.py` | Load/save YAML config files | `ConfigLoader`, `get_config`, `load_config` |
| `models.py` | Pydantic models for config data | `DangoConfig`, `ProjectContext`, `SourcesConfig`, `DataSource` |
| `exceptions.py` | Config-specific exception hierarchy | `ConfigError`, `ConfigNotFoundError`, `ConfigValidationError` |
| `credentials.py` | Credential loading for dlt sources | `CredentialManager` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new config field | `models.py` | `pytest tests/unit/test_config_models.py` |
| Change config loading logic | `loader.py` | `pytest tests/unit/test_config_loader.py` |
| Add a new config error type | `exceptions.py` | `pytest tests/unit/test_config_loader.py` |
| Add a new credential provider | `credentials.py` | `pytest tests/unit/test_credentials.py` |

## Dependencies

**Imports from:**
- `pydantic` — model validation (`BaseModel`, `Field`, `validator`)
- `yaml` — YAML parsing in `loader.py`
- `toml` — TOML parsing in `credentials.py`

**Used by:**
- `dango/cli/` — loads config to drive CLI commands
- `dango/ingestion/` — reads source definitions for sync jobs
- `dango/web/` — serves config through API endpoints
- `dango/oauth/` — reads OAuth credential config

## Testing

- **Unit:** `pytest tests/unit/test_config_loader.py tests/unit/test_config_models.py`
- **Integration:** `pytest tests/integration/test_config_loading.py`
- **Manual:** `dango config show` in a dango project directory

## Don't Modify

| File | Reason |
|------|--------|
| `models.py` field names | Changing field names breaks existing project.yml/sources.yml files |
```
