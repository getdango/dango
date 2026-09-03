# dango/

## Purpose

Root Python package for Dango — contains all module subpackages (cli, config, ingestion, etc.) and shared top-level infrastructure (logging, package metadata).

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Package metadata: version, author, license | `__version__`, `__author__`, `__license__` |
| `logging.py` | Structured logging (structlog + stdlib integration) | `configure_logging()`, `get_logger()`, `bind_contextvars`, `clear_contextvars`, `unbind_contextvars` |
| `telemetry.py` | Opt-in anonymous install + weekly heartbeat telemetry (machine-level UUID in `~/.dango/`); also owns dbt/dlt provider state read/write for the unified `dango telemetry` control + `/settings/telemetry` web page (1.0.8-U) | `is_ci()`, `is_telemetry_enabled()`, `has_recorded_consent()`, `set_telemetry_enabled()`, `ping()`, `heartbeat()`, `heartbeat_job()`, `get_dbt_telemetry_state()`, `set_dbt_telemetry_state()`, `get_dlt_telemetry_state()`, `set_dlt_telemetry_state()`, `PROVIDERS` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Change package version | `__init__.py` (also update `pyproject.toml`) | `python -c "import dango; print(dango.__version__)"` |
| Configure logging | `logging.py` | `pytest tests/unit/test_logging.py` |
| Add a new module | Create `{module}/` dir with `__init__.py` and `CLAUDE.md` | `python scripts/validate_claude_md.py dango/{module}/CLAUDE.md` |
| Find which module handles a task | Read root `CLAUDE.md` routing table | — |

## Dependencies

**Imports from:**
- `structlog` — structured logging in `logging.py` (also uses stdlib `logging`, `pathlib`)

**Used by:**
- `dango/cli/main.py`, `dango/cli/init.py`, `dango/cli/wizard.py` — import `__version__`
- `dango.logging` — `get_logger()` used by `web/routes/`, `web/middleware/`, `web/app.py`, `platform/scheduling/`, `platform/notifications/`, `oauth/web_flow.py`, `utils/log_rotation.py`, `utils/post_sync.py`, `cli/commands/schedule.py`, `cli/commands/remote_env.py`, `governance/`, `analysis/`; `configure_logging()` called by entry points. Note: `notebooks/` uses stdlib `logging` directly, not `dango.logging`.
- `pyproject.toml` defines `getdango` as the installable package
- Entry point: `dango.cli.main:cli` (configured in `pyproject.toml`)
- `dango.telemetry` — `cli/init.py` (`ProjectInitializer._prompt_telemetry_consent()`) fires the install ping; `platform/scheduling/scheduler.py` (`SchedulerService._setup_telemetry_heartbeat()`) registers `heartbeat_job()` as a weekly internal scheduler job. Stdlib `urllib.request`/`json`/`platform`/`uuid` only, plus lazy `yaml` for the opt-out config file and a lazy `dango.config.helpers.get_config` import inside `heartbeat_job()`. No new pip dependency. `cli/commands/telemetry.py` and `web/routes/telemetry.py` (1.0.8-U) both call `get_dbt_telemetry_state()`/`set_dbt_telemetry_state()`/`get_dlt_telemetry_state()`/`set_dlt_telemetry_state()`/`PROVIDERS` — this is the one real implementation both front-ends share (Level 0, so `web/` at Level 2 can import it without a Level-2-imports-Level-3 violation).

## Testing

- **All tests:** `pytest`
- **Unit only:** `pytest -m unit`
- **Integration only:** `pytest -m integration`
- **Logging tests:** `pytest tests/unit/test_logging.py`
- **Telemetry tests:** `pytest tests/unit/test_telemetry.py tests/unit/test_cli_init_telemetry.py`

## Don't Modify

| File | Reason |
|------|--------|
| `__init__.py` `__version__` | Version is also in `pyproject.toml` — update both together |
| Module `__init__.py` exports | Other modules depend on re-exported symbols |
