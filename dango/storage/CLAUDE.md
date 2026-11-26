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
| `workspaces.py` | Canonical operation replay, workspace leases/CAS, retention pins and explicit owned expiry | `WorkspaceService`, `Workspace`, `WorkspacePin`, `WorkspaceError` |
| `workspace_records.py` | Strict public records and immutable private recovery snapshots | Internal records; public records imported through `workspaces.py` |
| `workspace_state.py` | Private no-follow manifests, persistent locks and owner-scoped journal joins | Internal helpers |
| `CLAUDE.md` | Storage ownership and verification routing | None |

## Common Tasks

| To... | Modify... | Verify with... |
| --- | --- | --- |
| Change binding or authorization behavior | `catalogs.py` and assigned unit tests | `pytest tests/unit/test_catalog_bindings.py -m unit -q` |
| Change physical ownership/recovery | `catalog_ownership.py` and assigned unit/native tests | Interruption/substitution negatives and actual populated removal |
| Verify native catalog behavior | `tests/integration/native_catalog_bindings.py`, dedicated workflow | Explicit exact-pin native smoke, then the finite remaining matrix |
| Change workspace lifecycle/pins | `workspaces.py`, `workspace_state.py`, `workspace_records.py` | `pytest tests/unit/test_workspaces.py tests/unit/test_workspace_state.py tests/unit/test_workspace_replay.py tests/unit/test_operation_store_workspace.py -q` |
| Verify workspace/native join | `tests/integration/native_workspaces.py`, `workspace_cases.py`, `workspace_observations.py`, dedicated T04B workflow | Guarded target import/bootstrap, then populated workspace smoke and fixed selected cases |
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
The `affected` phase selects only08/12/16 after smoke18 for saved-snapshot identity checks.
Its distinct affected-success receipt leaves other current-head rows UNRUN and references the
retained f01d88b0 full run45673095810 and b2c18165 affected run45728157345 separately; it never
claims a fresh full-matrix pass. Replay regressions live in
`tests/unit/test_workspace_replay.py`; independent native oracles/selection constants live in
`tests/integration/workspace_observations.py` to keep setup and cases focused.

## Workspace lifecycle

`WorkspaceService(project_root, allocator, store, clock)` takes an aware UTC server clock.
`allocate(context, branch_ref, code_commit, location)` requires explicit remote/local location;
`get(context, workspace_id)`, `extend(context, workspace_id, expected_revision, expiry)` and
`expire(context, workspace_id, expected_revision)` check the owning project/actor and admitted
canonical operation. Initial lease is provisionally seven days for this unregistered service;
this is not shipping policy or automatic deletion. Near-expiry warnings are computed from the
stored expiry by callers. Extension only lengthens a lease and uses a strict integer CAS.

The server first admits `workspace.allocate` payload `{branch_ref,code_commit,location}`,
`workspace.extend` payload `{workspace_id,expiry}` or `workspace.expire` payload `{workspace_id}`.
Extension/expiry use decimal `expected_revision`; expiry strings use UTC ISO microseconds/+00:00.
The service compares the admitted request digest and uses the returned canonical operation ID.
Idempotency keys remain request-owned. One prepared allocation effect journals the random ID
before native IO; branch names never select authority or physical resources. Ready is exposed
only after durable catalog preparation and effect application. Missing late authority refuses
reconstruction; valid early interruption resumes the recorded ID.
New extensions enforce the same allocation-application readiness boundary as get; previously
recorded extension results replay exactly. Expiry refreshes final receipts under its operation
lock, including a waiter whose eligibility changes while an earlier call finalizes blocked.
Saved allocation and historical change results must match their enclosing owner/resource and
immutable catalog/data references. Healthy replay preserves its original revision and lease
even after later extension or expiry; replay never substitutes the latest Workspace.

`acquire_pin(context,workspace_id,purpose,resource_ref)` returns an opaque `WorkspacePin` for
input/build/query/backup. The same operation/purpose/resource returns the same active pin.
`release_pin(context,workspace_id,pin_id)` is idempotent only for its owning operation, including
after finalization; released pins are retained as tombstones. `has_pins` refuses unknown state.
Pins outlive UI leases and operation finalization; released pins' still-active operations also
block expiry, as do referenced unresolved recovery outcomes. Future T05/T06B and other consumers
must call this API at their joins. Building state retains independently. Duplicate/missing keys
and contradictory private phase/state/revision/removal-owner records refuse cleanup. `has_pins`
validates every retained operation reference before reporting the actual pin boolean.

Persistent lock order is operation intent, workspace, then allocator. No SQLite transaction
spans native IO. Private state holds operation references, never copied operation lifecycle
authority. Expiry rechecks time/activity/pins under the workspace lock, reads T04A's current
binding through its private `OwnedWorkspace` journal, then always invokes public authorized
allocator removal, including an already-removed interrupted retry. Caught resumable failures
retain active operations and raise safe errors; finalized receipts never reopen. Kernel locks
and mode0700 roots coordinate supported callers, not hostile programs sharing the OS identity.

## Don't Modify

| Boundary | Reason |
| --- | --- |
| Package marker imports/exports/initialization | Must remain empty and import-safe |
| Shared operation envelopes, protected credential store or grant persistence | Owned by their consumer/server integration tasks |
| Product runtime admission, Python support or dependency declarations | Native development proof does not promote the candidate profile |
| Global schemas/databases, foreign paths or shared services | Reconciliation/removal require recorded current ownership |
