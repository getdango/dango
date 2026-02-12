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
- `structlog` — structured logging in `logging.py`

**Used by:**
- All submodules import from dango subpackages (config, utils, etc.)
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
