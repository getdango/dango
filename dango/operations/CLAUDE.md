# operations/

## Purpose

Shared lower-level operation envelopes, safe payload errors and deterministic local request
identity, plus a durable residual-operation journal. This package performs no authentication,
authorization or service startup.
An `OperationContext` carries already-authenticated server inputs; constructing it grants no access.
The server owns actor, project, capability and permission revision. A nested payload field named
`actor` is domain data and cannot become server identity.

## Files

| File | Purpose | Public API |
| --- | --- | --- |
| `__init__.py` | Import-safe package marker | None |
| `contracts.py` | Strict schema-1 envelopes, payload validation and request hashing | `OperationContext`, `OperationRequest`, `OutcomeReceipt`, `ContractValidationError`, `canonical_request_bytes`, `request_digest` |
| `store.py` | Atomic admission, immutable final receipts, ordered effects and server-timed project leases | `OperationStore`, `BeginResult`, `EffectRecord`, `LeaseBinding` |

## Common Tasks

| To... | Modify... | Verify with... |
| --- | --- | --- |
| Change an envelope or identity rule | `contracts.py` and independent test oracles | `python -m pytest tests/unit/test_operation_contracts.py -x` |
| Change journal admission, effects or leases | `store.py` and independent SQLite/process oracles | `python -m pytest tests/unit/test_operation_store.py -x` |
| Update package guidance | `CLAUDE.md` | `python scripts/validate_claude_md.py dango/operations/CLAUDE.md` |

Schema version 1 belongs to this shared envelope. Incompatible field/state changes require a
coordinated schema-version and fixture update before integration. Domain payload schemas belong
to their owning operations; those handlers validate and authorize payloads. The server registry
owns mutation-specific idempotency requirements. This package does not trust client mutation or
approval flags and does not define roles.

## Dependencies

Contracts import only Python's standard library, Pydantic 2 and its existing `typing_extensions`
dependency. The store also imports the existing `get_connection` metadata helper and migration
exception. No imports from CLI, web, native clients or configuration loaders are permitted.
There are no registered product callers yet. Future server/adapter callers must authenticate and
authorize separately, snapshot request bytes where needed, and supply server-derived context.
`OperationStore(project_root, *, clock_epoch_us)` requires a server-owned integer microsecond clock.
Its methods are `begin_operation`, `mark_running`, `record_effect`, `finalize_operation`,
`acquire_lease`, `renew_lease` and `release_lease`. Metadata bootstrap owns the version-1 tables in
`.dango/dango.db`; the store uses short explicit transactions and performs no native IO. Leases
serialize journal ownership and do not fence native writers. Release preserves revision history;
cleanup may release a finalized operation's current lease. Finalized operations reject other writes.
Recovery uses a new operation linked through an effect's `recovery_ref`.

## Testing

Run `python -m pytest tests/unit/test_operation_contracts.py -x`, scoped Ruff lint/format checks
and package mypy. Tests use independent literal JSON and hashlib oracles, hostile inputs and a
fresh import subprocess that guards against filesystem writes, network and native initialization.
Run `python -m pytest tests/unit/test_operation_store.py -x` for concurrent admission, exact replay,
ownership, effect order, clock/lease transitions, interruption and payload canary checks. These
establish local durability and envelope behavior, not authenticated access or native recovery.

Frozen Pydantic models are shallow: nested payload dictionaries/lists remain mutable. Every hash
revalidates and serializes current payload values; never cache a digest on the model. JSON uses
Python's deterministic encoding rules, not RFC 8785 or a cross-language canonicalization promise.
Canonical bytes may contain sensitive payloads: do not persist or log them here. Pydantic hides
inputs in rendered validation errors; do not expose raw structured validation inputs to clients.
The store persists the request digest, never the raw payload. Receipt/effect references and error
text must already be safe before they reach the store; validation cannot recognize arbitrary secrets
inside free text. Store failures use fixed codes/actions and do not render database errors.

## Don't Modify

| Boundary | Reason |
| --- | --- |
| Root package exports/version, role tokens, dependencies and configuration | Outside shared-envelope ownership |
| Authentication/capability checks | Context construction is not access control |
| Domain models, journal schema, dispatcher and native bindings | Owned by their respective operations and later integration work |
