# storage/

## Purpose

Storage binding primitives for the selected PostgreSQL-backed DuckLake architecture with
local Parquet. The package marker remains empty and import-safe. The native adapter uses
the exact Python3.14/DuckDB2.0/DuckLake1.1 development recipe; it does not admit the candidate
runtime profile or change Dango's supported Python range.

`CatalogAllocator` joins a current server-owned authorizer, typed protected credential
resolver and persisted ownership record before native use. Construction of an
`OperationContext`, `SecretRef` or `CatalogBinding` grants no access. The actual authenticated
server caller and persisted grant integration remain T09C's responsibility.

## Files

| File | Purpose | Public API |
| --- | --- | --- |
| `__init__.py` | Empty, import-safe package marker | None |
| `catalogs.py` | Strict binding records, authorization/credential join and concrete native adapter | `CatalogBinding`, `CatalogAllocator`, `NativeCatalogPort`, `CatalogError` |
| `catalog_ownership.py` | Private manifest/root identity, no-follow IO and durable allocation/removal recovery | Internal ownership helpers |
| `CLAUDE.md` | Storage ownership and verification routing | None |

## Common Tasks

| To... | Modify... | Verify with... |
| --- | --- | --- |
| Change binding or authorization behavior | `catalogs.py` and assigned unit tests | `pytest tests/unit/test_catalog_bindings.py -m unit -q` |
| Change physical ownership/recovery | `catalog_ownership.py` and assigned unit/native tests | Interruption/substitution negatives and actual populated removal |
| Verify native catalog behavior | `tests/integration/native_catalog_bindings.py`, dedicated workflow | Explicit exact-pin native smoke, then the finite remaining matrix |
| Update this guide | `CLAUDE.md` | `python scripts/validate_claude_md.py dango/storage/CLAUDE.md` |

## Dependencies

`catalogs.py` uses Pydantic, `OperationContext`, `SecretRef` and the ownership helper. Native
DuckDB is imported only inside actual native use; `DuckDBPyConnection` is type-checking-only.
Import/collection never loads extensions, opens databases, creates state or reads credentials.
The concrete port is an internal implementation detail; callers use the allocator's safe
error and authorization boundary. Credentials accept exactly host/port/dbname/user/password.
Resolve only the exact current typed reference after current authorization; None is denial.

The server supplies pre-existing private0700 state/data roots, a fixed database/bundle and
trusted authorizer/resolver independently of request input. The resolver matches
`NativeCredentialStore.load(SecretRef)` mapping/None semantics and does not authenticate.
The authorizer receives context, persisted binding and allocate/read/write/reconcile/remove
action. It must check current grants/revocation at every operation. The workspace lock is
held throughout native connection use. Raw native connections are not SQL sandboxes or
per-statement revocation, cross-role writer fencing or customer security isolation.

Allocation journals intent before creating a root or namespace. Reuse binds the physical
root device/inode and PostgreSQL namespace owner/token/OID and metadata relation OIDs.
Reconciliation resumes only recorded owned phases; ambiguous gaps retain recovery evidence.
Removal records metadata-bound physical file identities before a single atomic PostgreSQL
statement locks owned tables, rechecks snapshot/ownership and drops exact tables plus schema
with RESTRICT. Its outer SQL body delimiter excludes all quoted path/value contents.
A foreign object makes the statement fail atomically. Physical deletion uses
no-follow descriptors and an inventory checkpoint; retries refuse additions/substitutions.
A redacted tombstone prevents identity reuse. Private roots and the lock coordinate supported
writers; this does not isolate other programs running as the same operating-system user.

## Testing

Focused checks: ruff/format on both modules and assigned tests; `mypy dango/`; unit binding
checks; guide/header/docstring/file-size/package checks. All Python files remain<=500 lines.
The dedicated native workflow is restricted to the task branch and six paths, with one
concurrency group and explicit bounded Linux amd64 controls. It is not every-PR CI.
Its first smoke verifies the actual adapter under a verified ordinary PostgreSQL role,
populated sales.orders350/finance.orders7, durable read-only reopen, raw metadata stability,
actual write denial and populated cleanup. Local doubles do not establish native acceptance.
The final bounded job gates its finite integration matrix on that smoke's success, then
checks owned crash recovery, foreign/concurrent additions, current access/refusal and canary
redaction. It retains separate smoke and per-case PASS/FAIL/UNRUN receipts. Acquisition uses
the unchanged hash-bound fixture and approved import pins with an identifying request header.
The initializer import closure includes logging/console dependencies. A restricted-import
regression prevents a fuller host site from masking omissions; it is not native acceptance.
Supported native-platform evidence is Linux amd64 only; macOS/Windows native behavior is unproved.

## Don't Modify

| Boundary | Reason |
| --- | --- |
| Package marker imports/exports/initialization | Must remain empty and import-safe |
| Shared operation envelopes, protected credential store or grant persistence | Owned by their consumer/server integration tasks |
| Product runtime admission, Python support or dependency declarations | Native development proof does not promote the candidate profile |
| Global schemas/databases, foreign paths or shared services | Reconciliation/removal require recorded current ownership |
