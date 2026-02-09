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

---

## 1. Incremental Adoption Policy

Standards apply incrementally — not retroactively to the entire codebase.

**Rules:**
- **New files** must follow all standards from creation.
- **Modified files** — apply standards to the code you touch, not the entire file. If you add a function, that function follows standards. You don't need to rewrite neighboring functions.
- **Existing files** are exempt until actively modified. TASK-084 creates an exemption registry tracking known violations.
- **Bulk reformatting PRs** are not allowed. Standards adoption happens organically through normal development.

**Example:** If you fix a bug in `dango/cli/main.py` (a ~4600-line file that predates these standards), apply standards only to the lines you change. You do not need to restructure the entire file — that happens in TASK-005.

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

**Good example — `dango/config/`** (5 files, each <200 lines, clear responsibilities):
```
config/
├── __init__.py      # Re-exports all public symbols
├── models.py        # DangoConfig, ProjectContext, SourcesConfig, enums
├── loader.py        # ConfigLoader class, load/save YAML
├── exceptions.py    # ConfigError hierarchy (4 classes)
└── credentials.py   # Credential encryption/decryption
```

**Counter-example — `dango/cli/main.py`** (~4600 lines, many responsibilities in one file). This is refactored by TASK-005.

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

### Current pattern (use until TASK-008)

The `dango/config/exceptions.py` hierarchy is the model for new code:

```python
# dango/config/exceptions.py — current pattern
class ConfigError(Exception):
    """Base exception for configuration errors"""
    pass

class ConfigNotFoundError(ConfigError):
    """Configuration file not found"""
    pass

class ConfigValidationError(ConfigError):
    """Configuration validation failed"""
    pass

class ProjectNotFoundError(ConfigError):
    """Not in a Dango project directory"""
    pass
```

**Rules for new code (before TASK-008):**
- Create a base exception per module (e.g., `IngestionError`, `OAuthError`)
- Subclass for specific error cases
- Never bare `except:` — always catch specific exceptions
- Always include context in error messages (what failed, what was expected)
- Catch-and-wrap external library exceptions at module boundaries

**Example — catch with context** (from `dango/config/loader.py`):

```python
try:
    with open(file_path, 'r') as f:
        data = yaml.safe_load(f)
        return data or {}
except yaml.YAMLError as e:
    raise ConfigError(f"Invalid YAML in {file_path}:\n{e}")
```

### Target pattern (TASK-008)

> **⚠️ TARGET PATTERN** — Not yet implemented. Do not use this pattern until TASK-008 lands.

```python
class DangoError(Exception):
    """Base for all dango errors."""
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        context: dict | None = None,
        user_message: str | None = None,
    ):
        self.error_code = error_code
        self.context = context or {}
        self.user_message = user_message or message
        super().__init__(message)

# Module exceptions inherit from DangoError
class ConfigError(DangoError):
    """Configuration-related errors."""
    pass
```

---

## 6. Logging Patterns

### Current pattern (use until TASK-007)

The codebase currently uses Rich `Console` for all output — there is no structured logging yet.

**Rules for new code (before TASK-007):**
- Continue using `from rich.console import Console` for user-facing output
- Add `# TODO(TASK-007): structured logging` where structured logging would be appropriate
- Do not introduce `logging` or `structlog` yet — TASK-007 sets up the infrastructure

### Target pattern (TASK-007)

> **⚠️ TARGET PATTERN** — Not yet implemented. Do not use this pattern until TASK-007 lands.

```python
import structlog

logger = structlog.get_logger(__name__)

# Structured logging (JSON to file)
logger.info("sync_completed", source="google_sheets", rows=1523, duration_s=4.2)
logger.error("sync_failed", source="stripe", error=str(e), retry_in=60)

# Rich console stays for user-facing output
from rich.console import Console
console = Console()
console.print("[green]Sync complete![/green]")
```

**Log levels:**
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

See the [__init__.py pattern](#initpy-pattern) in Section 2.

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
"""
dango/{module}/{filename}.py

{One-line purpose.}

Related files:
- {related_file} - {why}

Entry points:
- {function_or_class} - {description}
"""
```

**Required elements:** file path line, purpose line.
**Recommended elements:** related files, entry points (include when the file has non-obvious relationships or multiple public symbols).

### Incremental adoption

Currently 0 of ~151 Python files have headers. Headers are added when files are created or actively modified — not in a bulk update pass. The `scripts/validate_headers.py --all` audit mode tracks progress.

### Examples

**1. Module file** — `dango/config/loader.py`:

```python
"""
dango/config/loader.py

Loads and validates YAML configuration files for dango projects.

Related files:
- dango/config/models.py - Pydantic models this loader populates
- dango/config/exceptions.py - Errors raised during loading

Entry points:
- ConfigLoader - Main class for loading/saving config
- get_config() - Helper to load config in one call
"""
```

**2. Helpers file** — `dango/utils/database.py`:

```python
"""
dango/utils/database.py

DuckDB schema management utilities.

Related files:
- dango/transformation/ - dbt uses schemas created here

Entry points:
- ensure_dbt_schemas() - Creates raw/staging/intermediate/marts schemas
"""
```

**3. Test file** — `tests/unit/test_config_loader.py`:

```python
"""
tests/unit/test_config_loader.py

Unit tests for ConfigLoader YAML loading, saving, and project discovery.

Related files:
- dango/config/loader.py - Module under test
- tests/conftest.py - Shared fixtures (tmp_project_dir)
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
