# ADR-001: SQLite for Authentication

## Status
Accepted

## Context
Dango needs an authentication system to manage users, sessions, and roles. This auth layer also supports Metabase SSO — when a user logs into Dango, they are automatically logged into Metabase via session bridging (see VAL-001 findings).

The target deployment is a single server serving a small team (<100 users). The auth system must store user credentials, session tokens, role assignments, and encrypted Metabase passwords for SSO bridging.

## Decision
Use SQLite (`auth.db`) as the authentication database. Store users, sessions, and roles in SQLite tables. For Metabase SSO, generate a random Metabase password per user and store it encrypted in `auth.db` (Option A from VAL-001). The user never sees or manages their Metabase password — Dango uses it to create Metabase sessions on their behalf.

## Rationale
SQLite requires no external processes, no network configuration, and no additional deployment steps. It is a single file that deploys alongside the application. For the target scale of small teams (<100 concurrent users), SQLite's write throughput and connection model are more than sufficient.

VAL-001 validated that Metabase session bridging works: `POST /api/session` with stored credentials returns a session token that Dango sets as a `metabase.SESSION` cookie. Decoupling Metabase passwords from user passwords (Option A) means password changes don't require syncing between systems.

## Alternatives Considered
- **PostgreSQL:** Provides replication, concurrent write scaling, and mature tooling. However, it requires running a separate database server, which adds operational complexity for small teams. The capabilities it provides (high-concurrency writes, replication) are unnecessary at this scale.
- **External auth service (Auth0, Keycloak):** Offloads auth complexity but introduces an external dependency and recurring cost. Small teams deploying Dango on a single DigitalOcean droplet would need to manage an additional service or pay for a hosted tier.

## Consequences
- SQLite will not scale to thousands of concurrent write-heavy sessions. This is acceptable — Dango v1 targets small teams.
- No built-in replication. If the server is lost, `auth.db` must be restored from backup.
- Migration to PostgreSQL is feasible if a future version needs to scale beyond SQLite's limits. The schema and queries are standard SQL.
- Metabase passwords stored in `auth.db` must be encrypted at rest. Compromise of `auth.db` would expose encrypted Metabase credentials (but not user passwords, which are hashed).
