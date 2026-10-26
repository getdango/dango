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

## Common Tasks

| To... | Do... | Verify with... |
|-------|-------|----------------|
| Evolve metadata schema | Reserve the next global version and add `NNN_description.py` here | Metadata migration tests |
| Apply this stream in an isolated fixture | Prepare legacy schema with `get_connection(project_root)`, close it, then call `MigrationRunner(get_dango_db_path(project_root), "metadata", migrations_dir).apply_pending()` | Legacy preservation and once-only tests |
| Integrate metadata bootstrap | T03B owns serialized `apply_pending()` integration with `get_connection()` | T03B bootstrap/concurrency tests |

Preserve existing profiling, governance, notebook and analysis tables and rows.
Inventory any unexplained existing `_migrations` history before claiming compatibility;
a recorded version alone does not establish that the journal schema exists. Never
overwrite history or start another version 1 in a future feature folder.

The current generic migration orchestration maps a `metadata/` directory to
`.dango/metadata.db`. That routing blocks integration of this candidate until a
separately scoped change targets `.dango/dango.db`. Do not use generic orchestration
as proof of metadata bootstrap acceptance.

## Dependencies

- Imports: standard-library `sqlite3` only; upgrades receive an existing connection.
- Used by: explicit `MigrationRunner` calls in tests; T03B owns runtime bootstrap.
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
- `python -m pytest tests/unit/test_metadata_migration.py tests/unit/test_migrations.py tests/unit/test_dango_db.py -x`
- Tests use temporary SQLite projects, preserve legacy data, check the global version
  stream, keys, foreign keys, lifecycle constraints and independent failure rollback.

## Don't Modify

| Item | Reason |
|------|--------|
| Previously applied migration files or `_migrations` rows | Preserve database history; add the next global version instead |
| Existing legacy tables and data | The journal is additive to the existing metadata database |
| Auth/scheduler streams | Their physical databases and compatibility remain independent |
| Startup hooks, bootstrap or stores in this schema task | T03B owns serialized integration and operation-store behavior |
