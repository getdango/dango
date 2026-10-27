# semantics/

## Purpose

Import-safe package scaffold for semantic work. This scaffold provides no semantic
behavior, runtime qualification, state initialization or authentication.

Semantic consumers own their domain contracts and behavior. Shared schema-1 operation
envelopes remain owned by `dango/operations/contracts.py`; this package does not duplicate
or re-export them. Semantic records, adapters and their acceptance belong to their consumer
tasks.

## Files

| File | Purpose | Public API |
| --- | --- | --- |
| `__init__.py` | Empty, import-safe package marker | None |
| `CLAUDE.md` | Semantic ownership and verification guidance | None |

## Common Tasks

| To... | Modify... | Verify with... |
| --- | --- | --- |
| Update package guidance | `CLAUDE.md` | `python scripts/validate_claude_md.py dango/semantics/CLAUDE.md` |
| Implement semantic behavior | The files assigned to the owning consumer task | That task's independent contract and integration checks |
| Change a shared operation envelope | `dango/operations/contracts.py`, under its owner's coordinated schema change | Shared operation-contract tests and affected consumer fixtures |

## Dependencies

The package marker imports nothing and has no runtime dependencies or registered callers.
Importing `dango.semantics` must not load native clients, open databases, create state,
read credentials, authenticate, start services or contact external systems.
Future semantic consumers must honor the shared operation envelopes and separately enforce
their authentication, authorization and integration requirements.

## Testing

Run `python scripts/validate_claude_md.py dango/semantics/CLAUDE.md`,
`ruff check dango/semantics/__init__.py` and
`ruff format --check dango/semantics/__init__.py`.
Verify a fresh `import dango.semantics` with filesystem writes, database/network/process
activity and authentication/runtime initialization denied, allowing ordinary import reads.
An empty package marker needs no behavioral suite. Consumer tasks own behavior tests;
guide and import checks do not establish native access, concurrency or recovery.

## Don't Modify

| Boundary | Reason |
| --- | --- |
| Package marker imports, exports or initialization | This scaffold must remain empty and import-safe |
| Shared operation envelopes and schema version | Owned by `dango/operations/contracts.py` and its coordinated consumers |
| Semantic records, runtime adapters, persistent state, credentials and service lifecycle | Owned by separately scoped consumer tasks |
| Root routing guides, dependencies and configuration | Outside this package scaffold's ownership |
