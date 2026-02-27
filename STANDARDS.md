# Dango Code Standards

> **Tool Configuration:** All tool-enforced rules (line length, lint rules, mypy strictness) are defined in `pyproject.toml`. This document covers patterns and conventions that tools cannot enforce. Do not duplicate tool configuration here — reference `pyproject.toml` as the source of truth.

> **Incremental Adoption:** These standards apply to **new files** and **files being actively modified**. Existing files are not bulk-updated. See [Section 1](#1-incremental-adoption-policy) for details.

## Table of Contents

1. [Incremental Adoption Policy](#1-incremental-adoption-policy)
2. [File Organization](#2-file-organization)
3. [Naming Conventions](#3-naming-conventions)
4. [Import Ordering](#4-import-ordering)
5. [Error Handling Patterns](#5-error-handling-patterns)
6. [Logging Patterns](#6-logging-patterns)
7. [Testing Patterns](#7-testing-patterns)
8. [Documentation Requirements](#8-documentation-requirements)
9. [Type Hints Policy](#9-type-hints-policy)
10. [File Header Standard](#10-file-header-standard)
11. [Authentication & Web Patterns](#11-authentication--web-patterns)
12. [Development Workflow](#12-development-workflow)
13. [Compatibility Notes](#13-compatibility-notes)
14. [CI/CD](#14-cicd)

---

## 1. Incremental Adoption Policy

Standards apply incrementally — not retroactively to the entire codebase.

**Rules:**
- **New files** must follow all standards from creation.
- **Modified files** — apply standards to the code you touch, not the entire file. If you add a function, that function follows standards. You don't need to rewrite neighboring functions.
- **Existing files** are exempt until actively modified. TASK-084 creates an exemption registry tracking known violations.
- **Bulk reformatting PRs** are not allowed (TASK-088 was the one-time exception for initial mechanical compliance). Standards adoption happens organically through normal development.

**Example:** If you fix a bug in `dango/cli/main.py` (a ~3900-line file that predates these standards), apply standards only to the lines you change. You do not need to restructure the entire file — that happens in TASK-005.

---

## 2. File Organization

### Module structure

Each module (directory with `__init__.py`) should follow this layout:

```
module/
├── __init__.py         # Public exports + __all__
├── models.py           # Data classes, Pydantic models, enums
├── exceptions.py       # Module-specific exception classes
└── {responsibility}.py # One file per distinct responsibility
```

**Good example — `dango/config/`** (5 files, each focused on one responsibility):
```
config/
├── __init__.py      # Re-exports all public symbols
├── models.py        # DangoConfig, ProjectContext, SourcesConfig, enums
├── loader.py        # ConfigLoader class, load/save YAML
├── exceptions.py    # ConfigError hierarchy (4 classes)
└── credentials.py   # Credential encryption/decryption
```

**Counter-example — `dango/cli/main.py`** (~3900 lines, many responsibilities in one file). This is refactored by TASK-005.

### File size

- **Soft limit:** 500 lines per file.
- **When to split:** A file exceeds 500 lines, or contains 3+ unrelated responsibilities.
- **How to split:** Extract related functions/classes into a new file within the same module. Update `__init__.py` exports.

### `__init__.py` pattern

Every module's `__init__.py` should have: a module docstring, imports of all public symbols, and an explicit `__all__` list.

From `dango/config/__init__.py`:

```python
"""
Dango Configuration Module

Handles loading, validating, and managing Dango configuration files.
"""

from .models import (
    DangoConfig,
    ProjectContext,
    SourcesConfig,
    DataSource,
    SourceType,
    DeduplicationStrategy,
)
from .loader import ConfigLoader, get_config
from .exceptions import (
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
    ProjectNotFoundError,
)

__all__ = [
    # Models
    "DangoConfig",
    "ProjectContext",
    "SourcesConfig",
    "DataSource",
    "SourceType",
    "DeduplicationStrategy",
    # Loader
    "ConfigLoader",
    "get_config",
    # Exceptions
    "ConfigError",
    "ConfigNotFoundError",
    "ConfigValidationError",
    "ProjectNotFoundError",
]
```

---

## 3. Naming Conventions

| Element | Convention | Example |
|---------|-----------|---------|
| Files | `snake_case.py` | `config/loader.py`, `utils/database.py` |
| Classes | `PascalCase` | `ConfigLoader`, `DangoConfig`, `OAuthManager` |
| Functions/methods | `snake_case` | `find_project_root`, `load_yaml`, `ensure_dbt_schemas` |
| Constants | `UPPER_SNAKE_CASE` | `DANGO_DIR`, `PROJECT_FILE`, `SOURCES_FILE` |
| Private members | `_prefix` | `_get_encryption_key`, `_deep_merge`, `_clean_pasted_input` |
| Enums | `PascalCase` class, `UPPER_SNAKE_CASE` members | `DeduplicationStrategy.LATEST_ONLY` |

**Examples from codebase:**

```python
# Constants (dango/config/loader.py)
class ConfigLoader:
    DANGO_DIR = ".dango"
    PROJECT_FILE = "project.yml"
    SOURCES_FILE = "sources.yml"

# Enum (dango/config/models.py)
class DeduplicationStrategy(str, Enum):
    """Data deduplication strategies"""
    NONE = "none"
    LATEST_ONLY = "latest_only"
    APPEND_ONLY = "append_only"
    SCD_TYPE2 = "scd_type2"
```

---

## 4. Import Ordering

Import order is enforced by ruff's `"I"` (isort) rule — see `pyproject.toml` `[tool.ruff.lint]`.

**Order:** stdlib → third-party → dango

```python
# stdlib
import os
from pathlib import Path
from typing import Optional

# third-party
import yaml
from pydantic import ValidationError

# dango
from dango.config.models import DangoConfig
from .exceptions import ConfigError, ConfigNotFoundError
```

**Rules:**
- Relative imports are OK within the same module: `from .models import DangoConfig`
- Absolute imports for cross-module: `from dango.config import ConfigLoader`
- One import block per category, separated by blank lines
- ruff auto-fixes ordering on save — no manual effort needed

---

## 5. Error Handling Patterns

All Dango exceptions live in `dango/exceptions.py` and inherit from `DangoError`.
Module files (e.g., `config/exceptions.py`) re-export for backward compatibility.

### DangoError base class

```python
from dango.exceptions import DangoError, ConfigError, ConfigNotFoundError

# Every exception carries:
#   error_code  — stable identifier (e.g. "DANGO-C002")
#   context     — dict for structured logging / API responses
#   user_message — human-readable (defaults to message)

# Existing raise sites work unchanged — _default_error_code is a class attribute:
raise ConfigError("Missing sources.yml")
# ↑ error_code = "DANGO-C001" (the class default)

# Pass explicit error_code / context when useful:
raise ConfigNotFoundError(
    "sources.yml not found",
    context={"path": str(sources_file)},
)
```

### Import convention

Prefer importing from `dango.exceptions` directly:

```python
from dango.exceptions import ConfigError, ConfigNotFoundError
```

Old-style imports still work via re-export shims:

```python
from dango.config.exceptions import ConfigError  # same class object
```

### Rules

- Never bare `except:` — always catch specific exceptions
- Always include context in error messages (what failed, what was expected)
- Catch-and-wrap external library exceptions at module boundaries
- Use `from e` (not `from None`) unless suppressing the chain is intentional (KeyboardInterrupt handlers, security boundaries, user-facing error translations)

### Debug mode (`DANGO_DEBUG`)

Set `DANGO_DEBUG=1` to surface full stack traces in CLI error handlers.

```python
except Exception as e:
    console.print(f"[red]Error:[/red] {e}")
    from dango.exceptions import is_debug_mode  # lazy import inside handler

    if is_debug_mode():
        import traceback

        console.print(traceback.format_exc())
    raise click.Abort() from e
```

### Input validation (dango/validation.py)

```python
from dango.validation import validate_source_name, validate_date_string

validate_source_name(name)       # raises InvalidSourceNameError
validate_date_string("2024-01-15")  # raises InvalidDateFormatError
```

### API and async error patterns

**Never `HTTPException(detail=str(e))`** — this leaks internal implementation details (stack frames, SQL, file paths) to API consumers.

```python
# BAD — leaks internals
except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))

# GOOD — human-readable message
except Exception as e:
    logger.error("sync_failed", source=name, error=str(e))
    raise HTTPException(status_code=500, detail="Source sync failed")
```

**"Never raises" contract** — any async function documented as "never raises" must wrap its **full body** in `try/except Exception` with `logger.warning`. Internal error handling (e.g., `return_exceptions=True` in `asyncio.gather`) is insufficient — setup code before the guarded section can still propagate. See `auth/metabase_bridge.py`, `platform/notifications/webhook.py`.

**HTTP retry baseline** — use `_RETRYABLE_ERRORS = (httpx.TimeoutException, httpx.ConnectError)` as the retry tuple. Connection errors (reset, DNS blip) are the most common retry-worthy failures. Don't limit retries to only 5xx status codes + timeout. See `platform/notifications/webhook.py`.

---

## 6. Logging Patterns

Structured logging is provided by `dango/logging.py` (structlog wrapping stdlib). Rich Console stays for user-facing CLI/web output.

### Structured logging

```python
from dango.logging import get_logger

logger = get_logger(__name__)

# Structured logging (JSON to file, human-readable to stderr)
logger.info("sync_completed", source="google_sheets", rows=1523, duration_s=4.2)
logger.error("sync_failed", source="stripe", error=str(e), retry_in=60)
```

### Rich Console (user-facing output)

```python
from rich.console import Console
console = Console()
console.print("[green]Sync complete![/green]")
```

Use Rich Console for interactive CLI output (progress bars, tables, styled text). Use structured logging for operational events that should be recorded to log files.

### Entry point setup

Call `configure_logging()` once at each entry point (CLI, web server) before any logging occurs:

```python
from dango.logging import configure_logging
configure_logging()  # uses DANGO_LOG_LEVEL env var or defaults to INFO
```

### Log levels

- `DEBUG` — internal state, useful for troubleshooting
- `INFO` — normal operations (sync started, sync completed)
- `WARNING` — recoverable issues (retry, fallback used)
- `ERROR` — failures requiring attention

---

## 7. Testing Patterns

> Testing infrastructure is set up by TASK-001 (directory structure) and TASK-002 (mock factories). This section defines the conventions those tasks implement.

**Framework:** pytest

**Directory structure:**

```
tests/
├── conftest.py              # Shared fixtures
├── unit/
│   ├── conftest.py          # Unit test fixtures
│   ├── test_config_loader.py
│   └── test_config_models.py
└── integration/
    ├── conftest.py          # Integration test fixtures
    └── test_config_loading.py
```

**Naming:**
- Test files: `test_{module}.py` (e.g., `test_config_loader.py`)
- Test functions: `test_{behavior}` (e.g., `test_load_yaml_raises_on_missing_file`)
- Fixtures: descriptive names in `conftest.py` (e.g., `tmp_project_dir`, `sample_config`)

**Fixture pattern:**

```python
# tests/conftest.py
@pytest.fixture
def tmp_project_dir(tmp_path: Path) -> Path:
    """Create a temporary dango project directory."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    # ... set up project.yml, sources.yml
    return tmp_path
```

**Rules:**
- Unit tests have no I/O, no network, no database — use mocks and fixtures
- Integration tests may use temp directories and real DuckDB files
- Each test function tests one behavior — name describes the scenario
- Use `pytest.raises` for expected exceptions, not try/except in tests
- Factory pattern for test data (TASK-002 creates `tests/factories/`)

### Markers and organization

Apply markers (`@pytest.mark.unit`, `@pytest.mark.integration`) on the **class**, not on individual test methods. No `__init__.py` in test directories except `tests/__init__.py` and `tests/factories/__init__.py`. Integration test files use the `_integration` suffix (e.g., `test_auth_middleware_integration.py`) — cross-reference [§11](#11-authentication--web-patterns) for naming rules.

### Factory pattern

Test data factories live in `tests/factories/config_factories.py`. Use `make_*()` naming (e.g., `make_config()`, `make_source()`). The `unit` marker allows `tmp_path` usage — no external services or network, but filesystem is OK.

### Mocking and patching

- **Lazy imports → patch at origin.** When target code uses function-body imports (`from dango.config import X` inside a function), patch the source module (`dango.config.X`), not the consuming module. When code uses module-level imports, patch the consuming module's reference.
- **MagicMock base + explicit AsyncMock** for objects with both sync and async methods. Don't rely on `MagicMock` auto-detecting coroutines.
- **Shallow copy trap:** `dict()` / `{**d}` shares nested objects. Use `copy.deepcopy()` in fixtures that return mutable nested structures.
- **Mocking psutil:** Set `mock_psutil.NoSuchProcess = psutil.NoSuchProcess` — the exception class must be wired on the mock for `except psutil.NoSuchProcess` to work.
- **`dango.utils.dbt_lock` module/function collision:** `dango/utils/__init__.py` exports a function `dbt_lock` that shadows the submodule `dango.utils.dbt_lock`. On Python 3.10, `patch("dango.utils.dbt_lock.DbtLock")` resolves to the function, not the module. Fix: `import dango.utils.dbt_lock; _mod = sys.modules["dango.utils.dbt_lock"]` then `patch.object(_mod, "DbtLock", ...)`.

### TestClient gotchas

- **`TestClient.delete()` doesn't support `json=`** — use `client.request("DELETE", url, json=...)` instead.
- **Multi-router tests** must call `app.include_router()` for each needed router before creating the `TestClient`.
- **Cookie + middleware:** httpx cookie jar doesn't reliably populate raw ASGI cookie headers. Pass cookies via explicit `Cookie` header in integration tests: `headers={"Cookie": f"dango_session={token}"}`.

### Test file sizing

Split test files at creation (~300 lines target), not after hitting 500. Review rounds add ~30-50% more tests. When a plan estimates 200+ test lines, create two files upfront. Natural split: models/validation vs. loading/integration.

### Regression and verification

- **Silent corruption rule:** When fixing a bug that doesn't raise an error (just produces wrong data), verify a test fails if the fix is reverted. If not, the fix is untested.
- **When changing a default, grep tests AND templates** for the old behavior — `grep -rn "old_pattern" tests/ dango/web/templates/`. API response format changes can break template JS silently (unit tests don't catch this).
- **Define error messages as constants** when multiple code paths handle the same failure (e.g., API route + page route diverging on the same error string).

### Integration test requirements

Session-creation paths need integration tests — unit tests with mocked middleware miss security gaps (OAuth 2FA bypass, missing Metabase bridge). Any task adding a new login/session flow should include at least one integration test.

### Advanced patterns

- **Rich ANSI codes in CliRunner output:** CLI tests with Rich `console.print()` embed ANSI escapes. Strip before asserting: `_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m"); plain = _ANSI_RE.sub("", result.output)`.
- **`time.monotonic()` polling loops:** For functions with internal polling (e.g., `verify_health`), prefer patching the function itself rather than its clock dependency. If you must patch the clock, accept a `clock` callable parameter.
- **Digit-prefixed migration files:** Can't `import dango.migrations.scheduler.001_execution_history` (invalid Python identifier). Use `importlib.util.spec_from_file_location()` to load and call `upgrade()`.
- **New test files need mypy exemptions** under current config (test methods lack type annotations → `disallow_untyped_defs` fails). Add `ignore_errors = true` to `pyproject.toml` under the new file's mypy override. This is a known limitation — exempt files should be fixed over time per [§14 Mypy-exempt files](#mypy-exempt-files).
- **APScheduler dual-patch testing:** APScheduler 3.x `AsyncIOScheduler` constructor validates jobstores via `isinstance(value, BaseJobStore)`. Must mock both `SQLAlchemyJobStore` AND `AsyncIOScheduler` — a `MagicMock` jobstore alone gets rejected.

---

## 8. Documentation Requirements

### Docstrings

All public functions and classes require Google-style docstrings with `Args`, `Returns`, and `Raises` sections as applicable.

**Function docstring** (from `dango/config/loader.py`):

```python
def load_yaml(self, file_path: Path) -> dict:
    """
    Load YAML file with error handling.

    Args:
        file_path: Path to YAML file

    Returns:
        Parsed YAML as dict

    Raises:
        ConfigNotFoundError: If file doesn't exist
        ConfigError: If YAML is invalid
    """
```

**Short docstring** — acceptable for simple methods:

```python
def is_dango_project(self) -> bool:
    """Check if current directory is a Dango project"""
```

**Class docstring:**

```python
class ConfigLoader:
    """Loads and validates Dango configuration"""
```

### Module `__init__.py`

Every `__init__.py` must have:
1. A module docstring (what the module does)
2. An explicit `__all__` list

See the [`__init__.py` pattern](#__init__py-pattern) in Section 2.

### Comments

- Explain **why**, not **what** — the code shows what, comments explain intent
- TODOs reference a task: `# TODO(TASK-007): add structured logging here`
- Remove commented-out code — use git history instead

---

## 9. Type Hints Policy

Type hints are enforced by mypy with `disallow_untyped_defs = true` — see `pyproject.toml` `[tool.mypy]`.

**Rules:**
- All function parameters and return types must be annotated
- Use modern syntax (Python 3.10+): `str | None` not `Optional[str]`, `list[str]` not `List[str]`
- Pydantic models get typed fields with `Field()` descriptions for important fields

**Examples from codebase:**

```python
# Function signatures (dango/config/loader.py)
def find_project_root(self, start_path: Optional[Path] = None) -> Optional[Path]:
    ...

def validate_config(self) -> tuple[bool, list[str]]:
    ...

# Pydantic fields with descriptions (dango/config/models.py)
class ProjectContext(BaseModel):
    """Project-level context and metadata"""
    name: str
    organization: Optional[str] = Field(
        None,
        description="Organization name (used in Metabase, Web UI, etc.)"
    )
    purpose: str = Field(description="Why this project exists, what it's used for")
    stakeholders: list[Stakeholder] = Field(default_factory=list)
```

> **Note:** Some existing code uses `Optional[str]` (pre-3.10 style). New code should use `str | None`. Existing files are updated incrementally per [Section 1](#1-incremental-adoption-policy).

### Gotchas

**Annotate `dict[str, Any]` when unpacking into Pydantic models.** Bare `{"key": "value"}` gets inferred as `dict[str, str]` by mypy, which fails when unpacked into typed constructors.

```python
from typing import Any

# BAD — mypy infers dict[str, str], unpacking into Pydantic fails
data = {"name": "test", "value": "123"}
config = MyModel(**data)

# GOOD — explicit annotation
data: dict[str, Any] = {"name": "test", "value": "123"}
config = MyModel(**data)
```

**`json.load()` / `toml.load()` return `Any`** — always annotate the result to get downstream type checking:

```python
result: dict[str, Any] = json.load(f)
```

---

## 10. File Header Standard

Every Python file should begin with a docstring header that identifies the file and its purpose. This helps LLMs and developers navigate the codebase quickly.

### Required format

```python
"""{path/to/file.py}

{One-line purpose.}
"""
```

**Required elements:** file path line, purpose line.
**Optional elements:** related files, entry points (include when the file has non-obvious relationships or multiple public symbols).

### Enforcement

All non-`__init__.py` Python files have STD-003-compliant headers (completed by TASK-088).
New files must include headers — CI enforces via `validate_headers.py` and pre-commit hook.

### Examples

**1. Module file** — `dango/config/loader.py`:

```python
"""dango/config/loader.py

Handles loading and validation of YAML configuration files.
"""
```

**2. Helpers file** — `dango/utils/database.py`:

```python
"""dango/utils/database.py

Database utilities for Dango projects.
"""
```

**3. Test file** — `tests/unit/test_config_loader.py`:

```python
"""tests/unit/test_config_loader.py

Tests for dango.config.loader — ConfigLoader and module-level functions.
"""
```

### Validation

Run the header validation script:

```bash
# Check specific files
python scripts/validate_headers.py dango/config/loader.py

# Check only files changed in current branch
python scripts/validate_headers.py --changed

# Audit all files (reports compliance, does not fail CI)
python scripts/validate_headers.py --all
```

---

## 11. Authentication & Web Patterns

Conventions established during Phase 2 (Authentication). These apply to `dango/auth/`, `dango/web/middleware/`, and auth-related web routes.

### Permission-gated endpoints

Use `require_permission()` as a FastAPI `Depends()` parameter. It returns the authenticated `User` object on success, or raises `AuthorizationError` (403).

```python
from dango.auth.permissions import require_permission

@router.get("/api/admin/users")
async def list_users(
    request: Request,
    user: User = Depends(require_permission("users.view")),
) -> JSONResponse:
    ...
```

### Cookie security flags

All auth cookies (`dango_session`, `metabase.SESSION`) must set:
- `httponly=True` — prevents JavaScript access
- `samesite="lax"` — CSRF protection for cross-site requests
- `secure=is_secure_request(request.scope)` — HTTPS-only when behind TLS

Use the `is_secure_request()` helper from `dango.web.middleware.auth` to detect TLS (checks `X-Forwarded-Proto` header and ASGI scope).

### Pydantic partial updates

Use `exclude_unset=True` (not `exclude_none`) when applying partial updates with Pydantic models. `exclude_none` breaks intentional `None` assignments on nullable fields.

```python
update_data = update_model.model_dump(exclude_unset=True)
```

### Structlog reserved keywords

Never use `event` or `timestamp` as keyword arguments in structlog calls — they conflict with structlog internals and cause silent data loss.

```python
# BAD
logger.info("something", event="login", timestamp="2026-01-01")

# GOOD
logger.info("something", audit_event="login", occurred_at="2026-01-01")
```

### Format-off for inline data

Use `# fmt: off` / `# fmt: on` around large inline data structures (frozensets, permission maps, enum blocks) to prevent ruff format from exploding line counts.

```python
# fmt: off
ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.ADMIN:  frozenset({"*"}),
    Role.EDITOR: frozenset({"source.view", "source.sync", ...}),
    Role.VIEWER: frozenset({"source.view", "dbt.view", ...}),
}
# fmt: on
```

### Validate-then-write ordering

In validation functions, perform all read-only checks before any writes. This prevents partial state corruption if a late check fails.

### `Callable[..., Any]` for FastAPI `Depends()` factories

FastAPI wraps dependency callables in coroutines. Typed function signatures (e.g., `Callable[[str], User]`) break when FastAPI adds its async wrapper. Use `Callable[..., Any]` for `Depends()` factory parameters.

### Login endpoint returns 400 (not 401)

Login endpoints (`POST /api/auth/login`) return **400** for bad credentials, not 401. The 401 status is reserved for "you need to authenticate" (missing/expired session), not "your credentials are wrong". This prevents browsers from showing native auth dialogs.

### Test file naming

Integration test files use the `_integration` suffix (e.g., `test_auth_middleware_integration.py`). Test directories lack `__init__.py`, so basenames must be globally unique across `tests/unit/` and `tests/integration/`.

---

## 12. Development Workflow

### Pre-commit validation

Before committing non-trivial changes, run the full check suite to catch everything in one pass (pre-commit hooks surface errors serially):

```bash
source venv/bin/activate
ruff check dango/ && ruff format --check dango/ && mypy dango/ && python scripts/check_file_sizes.py
```

### File-move checklist

Any time a file is moved or renamed, immediately check:
- `pyproject.toml` mypy exemptions for old path
- `docs/file-exemptions.yml` for old path
- CLAUDE.md files for old path references
- `grep -rn "old.module.path" dango/ tests/` for stale imports

### File size conventions

See [§2 File size](#file-size) for the 500-line soft limit and when to split. Additional workflow rules:

- If a new or modified file exceeds 500 lines, add it to `docs/file-exemptions.yml`.
- **Target ~430 lines pre-format** — ruff format expands code by 10-15%.
- **Incremental commits for large restructurings:** Tasks touching 10+ files should be split into 2-3 logical commits (e.g., "move files", "add new code", "update tests").

### Package registration

New subpackages must be added to the `packages` list in `pyproject.toml`. `pip install -e .` discovers packages automatically, but PyPI wheel builds don't — missing packages cause import failures for installed users.

### Template verification

After bulk find-and-replace operations on Jinja2 templates, grep to verify zero remaining occurrences of the old string. Automated replacements can silently miss matches in template syntax.

### Dependency awareness

Check `pyproject.toml` dependencies before writing utility code. PyYAML, httpx, toml, etc. are already available — don't hand-parse YAML with string splitting or write custom HTTP clients.

### Common gotchas

**`or default()` sentinel bug:** `param=None` then `param or make_default()` silently replaces explicit `None` with a default. Use `if param is None:` instead. Check test helpers and factory functions for this pattern.

**`__init__.py` export drift:** Verify `__init__.py` exports match the public API after adding new public functions. Missing exports are silent — internal tests import directly, but external consumers use the package interface.

---

## 13. Compatibility Notes

Dango supports Python >=3.10,<3.13. These patterns address cross-version compatibility:

**`datetime.fromisoformat()` timezone parsing:** Python 3.10 cannot parse timezone suffixes (`+00:00`, `Z`) — this was fixed in 3.11. Strip the suffix before parsing:

```python
# Works on Python 3.10+
ts = timestamp_str.replace("+00:00", "").replace("Z", "")
dt = datetime.fromisoformat(ts)
```

This affects backup manifests, scheduled backup metadata, and any serialized timestamps.

**`time.daylight` vs `time.localtime().tm_isdst`:** `time.daylight` is static ("does this timezone observe DST ever?"), while `.tm_isdst` is dynamic ("is DST active right now?"). Use `.tm_isdst > 0` for current offset calculations.

**`asyncio.get_running_loop()` not `get_event_loop()`:** `get_event_loop()` is deprecated in Python 3.12+. Always use `asyncio.get_running_loop()` inside async functions.

**`asyncio.to_thread()` for blocking calls:** Use `asyncio.to_thread()` to run blocking operations (database queries, file I/O, subprocess calls) from async handlers. Don't call blocking functions directly — they block the event loop.

---

## 14. CI/CD

### Blocking checks

These checks must pass for a PR to merge:
- `ruff check` (lint)
- `ruff format --check` (formatting)
- File header validation (`validate_headers.py`)
- Public function docstrings (`validate_docstrings.py`)
- File size check (`check_file_sizes.py` + `docs/file-exemptions.yml`)
- `pytest` (unit + integration tests)
- `mypy` (type checking)

### Soft checks

These run in CI but don't block merging (tracked for future enforcement):
- CLAUDE.md structure validation
- Orphan file detection
- Pinned dependency check

### New file requirements

Every new Python file must have:
- STD-003 file header (see [§10](#10-file-header-standard))
- Docstrings on all public functions (see [§8](#8-documentation-requirements))
- Pass ruff lint + format
- Pass mypy type checking

### Exception chaining (B904)

Default to `from e` for exception re-raises. Use `from None` only when suppressing the chain is intentional — see [§5](#5-error-handling-patterns) for the full rule (KeyboardInterrupt handlers, security boundaries, user-facing error translations).

### Mypy-exempt files

17 source files and ~50 test files currently have `ignore_errors = true` in `pyproject.toml`. Policy: fix ALL mypy errors when touching an exempt file — don't add new violations while the exemption is in place.
