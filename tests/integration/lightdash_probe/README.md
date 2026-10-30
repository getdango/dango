# Lightdash DuckLake client feasibility

This isolated, AI-assisted experiment uses public Lightdash 3.86.0 source at the
commit in `pins.json`. Its only upstream changes are the selected dependency
manifest and generated lockfile patch: DuckDB Node API/bindings 2.0.0-r.1.
Dango product code is unchanged. A successful run is feasibility evidence, not a
supported custom Lightdash build, a release qualification or a deployment.

The Ubuntu job first runs populated access and authenticated PostgreSQL readiness
checks under the same declarative Docker profiles used for execution. PostgreSQL
has no external network; Node shares its loopback namespace. Installation runs
alone without networking. Roots are read-only, capabilities are dropped, users
are numeric nonroot identities, and CPU, memory, swap, process counts, temporary
filesystems and logs are bounded. Failures and stale readiness block later work.

Pinned pnpm derives a dedicated production deployment lock from the frozen lock
without installation. The job verifies unchanged package resolutions and measures
selected tarball contents before standard fetching and offline installation with
lifecycle scripts disabled. Host acquisition paths stay writable by the host user;
explicit ownership handoffs restore UID501:20 and mode0755 before runtime and
invalidate readiness so the affected profiles must pass fresh smoke. It counts independent dependency copies, expanded
images, fixture/build/evidence reserves and 2GiB engineering headroom against
actual disk space and a 14GB task budget. Frozen layer measurements count every
planned image before pull: compressed cache plus three independent allocated
layer copies (immutable layers, decompression staging, container snapshot) and
64MiB metadata per image. Docker inspect Size is not an expansion bound. An unbounded or oversized graph fails.
The complete unchanged common source remains the compiler alias; no symbols or
side effects are stubbed. Compiler input metadata is exported.

A native producer creates a fresh synthetic PostgreSQL-backed DuckLake 1.1 catalog
with automatic migration disabled and three Parquet-backed amounts totaling 350.
The actual DuckdbWarehouseClient constructor/query path must identify engine2.x,
return350, reject UPDATE through READ_ONLY attach and reject a fresh incorrect
password with an authentication error. A correct-credential control runs again.
Whole catalog schema/data dumps (only random pg_dump restrict lines normalized),
catalog version and fixture byte hashes must remain unchanged. Extension and
native-library pins are checked. The native producer is not consumer evidence.

Only random synthetic secrets exist. They are masked before subprocess execution,
kept out of explicit container environments, checked in raw error/output bytes,
and removed during exact-owned cleanup. Sanitized commands, full container state
and HostConfig, logs/events, package inventory, compiler metadata and results are
the only artifact allowlist. No private historical probe trees or audit records
are runtime or public inputs. Workflow permissions are read-only and its PR guard
accepts only the authorized same-repository probe branch against `v1.1.0`.

Local checks are offline guard/static checks. Docker execution and all feasibility
oracles remain UNRUN until the reviewed workflow runs. Authorization/403, tenant
isolation, dlt/dbt, concurrency, rollback, restore and complete release qualification
are outside this probe. A first material failure stops the experiment for review.
