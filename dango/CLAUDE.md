# dango/

## Purpose

Root Python package for Dango — contains all module subpackages (cli, config, ingestion, etc.) and shared top-level infrastructure (logging, package metadata).

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Package metadata: version, author, license | `__version__`, `__author__`, `__license__` |
| `logging.py` | Structured logging (structlog + stdlib integration) | `configure_logging()`, `get_logger()`, `bind_contextvars`, `clear_contextvars`, `unbind_contextvars` |

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

## Testing

- **All tests:** `pytest`
- **Unit only:** `pytest -m unit`
- **Integration only:** `pytest -m integration`
- **Logging tests:** `pytest tests/unit/test_logging.py`

## Don't Modify

| File | Reason |
|------|--------|
| `__init__.py` `__version__` | Version is also in `pyproject.toml` — update both together |
| Module `__init__.py` exports | Other modules depend on re-exported symbols |
