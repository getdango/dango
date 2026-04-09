# migrations/

## Purpose

Incremental database migration framework for Dango's SQLite databases. Each subsystem (auth, scheduler, etc.) has its own subdirectory with independently versioned migration files. Migrations run automatically on `dango start` and can be managed manually via `dango migrate status` and `dango migrate run`.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Public API and subdirectory orchestration | `apply_all_pending()`, `get_all_status()` |
| `runner.py` | Core migration engine | `MigrationRunner`, `MigrationInfo`, `MigrationStatus`, `AppliedMigration` |

## Migration Subdirectories

Each subdirectory represents a database. Convention: `auth/` maps to `.dango/auth.db`.

```
migrations/
├── __init__.py     # Public API
├── runner.py       # Core engine
├── CLAUDE.md       # This file
├── auth/           # Created by TASK-011 (Phase 2)
│   ├── 001_initial_auth.py
│   ├── 002_complete_auth_schema.py
│   ├── 003_metabase_password.py
│   └── 004_invite_tokens.py
└── scheduler/      # Created by TASK-039 (Phase 4)
    └── 001_execution_history.py
```

## Migration File Contract

```python
"""dango/migrations/auth/001_initial_auth.py

Create initial auth tables.
"""
from __future__ import annotations
import sqlite3

VERSION = 1              # Must match NNN in filename
DESCRIPTION = "Create initial auth tables"

def upgrade(conn: sqlite3.Connection) -> None:
    """Apply this migration. Do not call conn.commit()."""
    conn.execute("CREATE TABLE IF NOT EXISTS users (...)")
```

Rules:
- Filename: `NNN_description.py` (zero-padded, e.g., `001`, `002`)
- `VERSION` int must match filename prefix
- `DESCRIPTION` string for status output
- `upgrade(conn)` receives an active connection inside a runner-managed transaction
- No `downgrade()` function
- `__init__.py` files are ignored during discovery
- Subdirectories have `__init__.py` for packaging (so setuptools includes them); the runner skips these files

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new migration | Create `NNN_*.py` in the appropriate subdirectory | `pytest tests/unit/test_migrations.py` |
| Add a new database | Create a new subdirectory under `migrations/` | `dango migrate status` |
| Check migration status | Run `dango migrate status` | — |
| Apply migrations manually | Run `dango migrate run` or `dango migrate run --db auth` | — |

## Dependencies

**Imports from:**
- `dango.exceptions` — `MigrationError`, `MigrationDiscoveryError`, `MigrationApplicationError`
- stdlib only: `sqlite3`, `importlib.util`, `pathlib`, `dataclasses`, `re`, `datetime`

**Used by:**
- `dango/cli/commands/platform.py` — auto-migrate on `dango start`
- `dango/cli/commands/migrate.py` — manual CLI commands

## Key Conventions

- **Testing digit-prefixed migration files:** Can't `import dango.migrations.scheduler.001_execution_history` (invalid Python identifier). Use `importlib.util.spec_from_file_location()` to load and call `upgrade()`. See `tests/unit/test_execution_history.py` for the pattern.

## Testing

- **Unit:** `pytest tests/unit/test_migrations.py -v`
- **Manual:** `dango migrate status`, `dango migrate run`

## Don't Modify

| File | Reason |
|------|--------|
| `_migrations` table schema | Applied migrations reference this schema; changing it breaks existing databases |
| `NNN_*.py` filename convention | Discovery relies on the regex pattern `^\d{3,}_.+\.py$` |
| Applied migration files | Once applied, migration files must not be altered (version is recorded in DB) |
