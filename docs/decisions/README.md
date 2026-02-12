# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for the Dango project. ADRs document significant architectural decisions, their context, and rationale so future contributors understand *why* choices were made.

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-001](ADR-001-sqlite-for-authentication.md) | SQLite for Authentication | Accepted |
| [ADR-002](ADR-002-apscheduler-over-celery.md) | APScheduler over Celery | Accepted |
| [ADR-003](ADR-003-duckdb-single-file-warehouse.md) | DuckDB as Single-File Warehouse | Accepted |
| [ADR-004](ADR-004-marimo-over-jupyterlite.md) | Marimo over JupyterLite | Accepted |
| [ADR-005](ADR-005-digitalocean-only-for-v1.md) | DigitalOcean Only for v1 | Accepted |
| [ADR-006](ADR-006-caddy-for-ssl.md) | Caddy for SSL | Accepted |
| [ADR-007](ADR-007-frontend-approach.md) | Frontend Approach | Accepted |

## Creating a New ADR

1. Copy the template below into a new file named `ADR-NNN-short-title.md`
2. Fill in all sections
3. Add an entry to the index table above
4. Submit via pull request

## Template

```markdown
# ADR-NNN: Title

## Status
[Proposed | Accepted | Deprecated | Superseded by ADR-NNN]

## Context
[What problem or need prompted this decision?]

## Decision
[What was decided?]

## Rationale
[Why this choice over alternatives?]

## Alternatives Considered
[What else was evaluated and why it was rejected?]

## Consequences
[What are the trade-offs, limitations, and follow-up implications?]
```
