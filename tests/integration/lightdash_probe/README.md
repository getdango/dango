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

Pinned upstream pnpm12.3.4 first lists its selected production graph directly
from the frozen lock. Before/after guards reject any dependency or store
materialization by this graph-only command. This is the actual v12 Rust list
implementation; the bundled legacy pnpm11 deploy implementation is irrelevant.
Linux command behavior remains pending until this gate executes in CI.

After the graph-only gate, standard `pnpm fetch --prod` acquires whole-workspace
production data into pnpm's own store/cache, with lifecycle scripts disabled and
without importer links. It is not claimed to be selected-graph fetch. The job
then measures all actual owned data/cache and runner free space before any offline
installation. Selected archives are checked against frozen integrity; the install
peak adds four unshared selected dependency copies, two measured-cache rewrite
copies and 2 GiB compiler/fixture/log allowance, plus the existing 2 GiB engineering
headroom and frozen image bounds. There is no arbitrary metadata-size prediction.
The temporary budget for this dedicated probe on the same runner is 18 GB
(18,000,000,000 bytes), even when the runner has more free disk. This approval
applies only to this probe; it is not a budget for every PR.

The explicit writable public-source copy preserves source bytes; a documented
workspace-warehouse symlink retains `/work/deploy`. Mandatory selected offline
installation and bounded compilation share the standard pnpm store/cache. No
resolver, registry, cache writer, source stub or native alias is added. Existing
ownership handoffs and fresh readiness checks remain mandatory. `pnpm view` is
not used to warm the install mirror: pinned 12 source fetches view metadata without
that cache path. Actual Linux graph/fetch/offline behavior and fit remain pending CI.
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
