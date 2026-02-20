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
