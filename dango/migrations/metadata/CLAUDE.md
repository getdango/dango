# migrations/metadata/

## Purpose

Own the single schema migration stream for the existing `.dango/dango.db` metadata
database. Versions increase globally and monotonically across all metadata features;
`MigrationRunner.db_name` is a diagnostic label and does not namespace `_migrations`.
Auth and scheduler migrations use separate physical databases and version streams.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Empty package marker; imports perform no initialization | — |
| `001_operation_journal.py` | Constrained operation, effect and lease tables | `VERSION`, `DESCRIPTION`, `upgrade()` |
| `bootstrap.py` | Persistent flock serialization and history checks for `.dango/dango.db` | `ensure_metadata_schema()` |

## Common Tasks

| To... | Do... | Verify with... |
|-------|-------|----------------|
| Evolve metadata schema | Reserve the next global version and add `NNN_description.py` here | Metadata migration tests |
| Open the runtime metadata database | Call `get_connection(project_root)`; it prepares legacy tables and runs the locked bootstrap before returning | Legacy preservation and bootstrap tests |
| Test a migration before bootstrap | Open SQLite directly and call the existing legacy `_ensure_schema()` in test-local setup, then use `MigrationRunner` | Metadata migration tests |
| Apply pending metadata migrations explicitly | Call `ensure_metadata_schema(project_root, timeout_seconds=10.0)` after legacy setup with no open SQLite write transaction | Bootstrap/concurrency tests |

Preserve existing profiling, governance, notebook and analysis tables and rows.
Inventory any unexplained existing `_migrations` history before claiming compatibility;
a recorded version alone does not establish that the journal schema exists. Never
overwrite history or start another version 1 in a future feature folder.

Generic migration apply/status orchestration skips `metadata/`; explicit CLI
metadata application is refused. Runtime bootstrap targets `.dango/dango.db`
and never creates `.dango/metadata.db`. It locks the persistent project-local
`.dango/metadata-migration.lock` with bounded nonblocking `fcntl.flock` waiting on
Linux/macOS. Never unlink that file: waiters must lock the same inode. Timeout
errors advise waiting/retrying or increasing the timeout. Kernel locks release
on failure or process death. Unexplained history is rejected without overwriting it.

## Dependencies

- Imports: standard-library SQLite/path/time/math and lazy `fcntl`; bootstrap uses
  `MigrationRunner` and `MigrationApplicationError`. Imports perform no initialization.
- Used by: `dango.utils.dango_db.get_connection()` (lazy bootstrap import) and tests.
  Failed preparation/bootstrap closes the caller connection; the process-local legacy
  cache never bypasses bootstrap or hides a failed migration.
- Runner owns `BEGIN`, commit and rollback. Use individual `conn.execute()` calls;
  never call `executescript()` or commit inside a migration.
- Final `status` values match T01 `OutcomeReceipt`: `success`, `blocked`, `failed`,
  `cancelled`, `recovery_required`. Earlier journal states have no final outcome.
- T03B stores must validate and redact the final receipt before storing `outcome_json`,
  make finalized receipts immutable, and use a new linked operation for recovery.
  Effect rows hold references, never raw credential or request payload dumps.
- Lease expiry is control-plane state. Native stale-writer fencing requires separate
  qualification; these constraints do not establish safe concurrent publication.

## Testing

- `python scripts/validate_claude_md.py dango/migrations/metadata/CLAUDE.md`
- `python -m pytest tests/unit/test_metadata_bootstrap.py -x`
- `python -m pytest tests/unit/test_metadata_migration.py tests/unit/test_migrations.py tests/unit/test_dango_db.py -x`
- Tests use temporary SQLite projects, preserve legacy data, check the global version
  stream, keys, foreign keys, lifecycle constraints and independent failure rollback.

## Don't Modify

| Item | Reason |
|------|--------|
| Previously applied migration files or `_migrations` rows | Preserve database history; add the next global version instead |
| Existing legacy tables and data | The journal is additive to the existing metadata database |
| Auth/scheduler streams | Their physical databases and compatibility remain independent |
| Operation stores or native writer fencing | Separate T03B2 and native qualification own these behaviors |
