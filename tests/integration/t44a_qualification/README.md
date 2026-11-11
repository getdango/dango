# Dedicated T44A first diagnostic

This qualification workflow runs only on the authorized same-repository branch
`test/t44a-full-native-03` against `v1.1.0` when its exact workflow/test paths change.
It is a temporary experiment, not an every-PR or product support requirement.

The immutable public R08 runtime is checked out at the commit in `pins.json`;
its identity/security/resource/capture/cleanup implementation is reused directly.
This is fresh Python3.14.10 readiness, not reuse of an old smoke PASS. One3CPU/3GiB
Python profile plus one1CPU/768MiB PG profile stays under4CPU/4.5GiB. The tiny
readiness bootstrap installs only the frozen offline wheel closure after a basic
populated Python/permissions/network smoke; then real Python DuckDB/postgres_scanner→PG authentication
must pass before the dbt/native fixture. This first phase stages no full Lightdash source.

Public acquisition has a minimal environment and exact size/hash pins. The
pre-install gate counts all planned frozen image expansions/caches/snapshots,
measured owned bytes, conservative materialized archive copies, explicitly
configured temporary/database/log/output peaks, and2GiB headroom within30GB AND
actual runner free space. The runtime namespace has only loopback, no published
ports or Docker socket, nonroot users, read-only roots and bounded CPU/RAM/tmpfs/logs.

Normal `dbc install` verifies the saved public driver signature and creates its
manifest under the task-owned `ADBC_DRIVER_PATH`. Real OSS dbt2.0.8 seed/table/test
and catalog output run against a fresh PG-backed DuckLake1.1 catalog with automatic
migration false and data inlining0. Three independent seed amounts100+200+50 imply
350, and September100+200 implies300. A real dbt singular test asserts engine2 and
total350; independent Python readback checks350/300 and PG checks spec1.1.
Only authentic sanitized manifest/run_results/catalog and diagnostic receipts are
exported. No synthetic password is embedded in the dbt project/metadata connection
string: libpq reads its mode0600 pgpass file. Raw output/artifacts are checked for
secret bytes before sanitization/publication; a leak fails the run.

Complete adapted Lightdash app, ordinary actor350/filter300/private403 and the
remaining matrix are **UNRUN**. A pass here only advances the first dbt/driver gate.
The app stage requires frozen Garage plus real PUT/GET smoke, NATS explicitly
disabled, and normal common/warehouse TypeScript builds before frontend work.
No source/API patch, proprietary dbt substitution or client validator bypass is
part of this experiment. No compiled app or private historical evidence is uploaded.

After the real dbt gate passes, the next checkpoint acquires the complete frozen
Lightdash development graph with standard pnpm12.3.4 `fetch`, with dependency
lifecycle scripts disabled. This is acquisition, not an app install/build. The
unchanged `.pnpmfile.cjs` metadata hook is separately hash-bound; ignoring lifecycle
scripts does not disable that trusted hook. Integrity-bound Linux/x64/glibc archive
inspection retains notices and native hashes before fetch. The acquisition fit
gate counts content-store, every expanded peer-context virtual-store copy, a full
temporary extraction copy and two compressed cache/temp copies, without hardlink
credit, plus measured owned bytes/images and2GiB headroom against30GB and actualdf.
Each measured archive member includes its existing4KiB metadata allowance in each
copy. Actual whole-store/cache/virtual-store sizes are measured after fetch, and
project importer links or changed frozen source inputs fail the checkpoint.

Only acquisition results and package size/license/native hashes join the public
allowlist. Original notices and fetched bytes stay in owned runner scratch for
the next gate; source archives and compiled apps are not uploaded. Missing licenses,
external lifecycle binaries and the full remaining install/compiler/runtime peak
still block installation. `app-profile.json` and `garage.toml` are proposed future
profiles, not executed app/Garage evidence or an installation approval.

The next gated stage resolves `pause@0.0.1` from its original MIT Readme and
`buffers@0.1.1` from the original author's fixed license-addition commit, whose
implementation matches the published archive. Exact notice/source hashes are in
`license-provenance.json`; neither published package reports an npm gitHead.
The build uses the approved4CPU/4608MiB profile, alone with no PG. The immutable
R08 helper first validates its created, unstarted original profile; a standard
exact-owned `docker update` changes only CPU/memory/equal-swap, then every other
HostConfig/Config/mount/identity/state field must remain unchanged before start.
A fresh populated Node permissions/loopback smoke also checks effective cgroup
CPU/memory/no-swap/pids limits. It then performs the full frozen **offline**
install with **all lifecycle scripts disabled**, followed by normal upstream
`formula:build`, `common-build`, `warehouses-build`, `visualization-build`,
`backend-build`, `frontend-build`, and `cli-build` commands, serially.
All seven targets passed in CI41319819062; app runtime remains UNRUN.
The lock-pinned Linux TypeScript compiler is already acquired and hash-bound.
No Cypress GUI, canvas/legacy DuckDB rebuild or unpinned binary download runs.
This deliberately leaves later app-runtime native initialization unproved; unused
test-tool downloads are not requirements for the app build. Normal build
commands retain their upstream pre/post hooks; dependency installation lifecycles
remain disabled.
Each install/compiler is preceded by30GB/actualdf checks, counting future unshared
materialization/cache copies and compiler/temp/log peaks separately from2GiB
headroom. Full command/state/log/event capture precedes exact-owned cleanup;
only build status, hashes, license provenance and sanitized receipts are exported.
The result binds every emitted output tree and requires backend server/routes/
migration, frontend index/build-hash plus JS/CSS assets, visualization CJS/ESM
and CLI entrypoints. App runtime, Garage and ordinary actors remain UNRUN.

The output/temp allowance is now8GiB:4GiB retained outputs/maps/tsbuildinfo and
4GiB temporary/compiler/plugin-cache copies, in addition to the unchanged
unshared materialization/cache-copy bounds and separate2GiB headroom. This is
a conservative engineering allowance, not an observed full-build measurement.
Actual frozen source is189,881,078bytes across11,923files; each compiler is
preceded by fresh owned-byte/image/df checks, and emitted outputs are measured.
A30GB/df refusal stops before the next command.

The preceding3GiB attempt completed install/formula but the common compiler hit
an observed container OOM. The fixed compiler binary uses Go1.26.4; its documented
`GOMEMLIMIT=3500MiB` soft runtime limit leaves room for pnpm/Node overhead under the
hard4608MiB container ceiling. Node uses the standard
`NODE_OPTIONS=--max-old-space-size=3500` for the remaining full build, with actual
heap size and environment checked by a fresh smoke and included in its signature.
The Go and Node soft limits do not add capacity or guarantee simultaneous fit;
the container hard aggregate remains4608MiB.
Compiler commands print own cgroup peak/events/limits before the shell exits;
the unchanged helper captures full container state/events/logs before removal.

AI assistance and authority: Codex authored this scratch qualification recipe under
the human-approved T44A batch. Independent review and coordinator verification are
required before publication/execution; no merge or product change is authorized.

CI41287421638 completed the early targets and visualization, then the backend
formatter hit the4.5GiB cgroup OOM limit. The backend recipe preserves normal
API generation, formatting, TypeScript compilation and postbuild asset copying
as serial upstream subcommands, adding only oxfmt0.35.0 `--threads 1` to formatting.
The exact release source applies this flag to Rayon and the Node child-process
formatter pool. No formatter bypass or source change is used. Individual killed
process identity was not captured; single-worker formatting passed in the later complete-build run.

CI41318651644 reached the subsequent `pnpm exec tsc` phase after single-worker
formatting, then the cgroup again recorded OOM. The backend compiler now uses
TypeScript7.0.2’s supported `--singleThreaded` with the same `--build` config.
The exact release reduces its checker pool from4to1 and serializes workgroups;
full diagnostics and emitted outputs remain required. Other targets and hard
limits are unchanged. Child PID/signal attribution was not separately captured;
single-threaded backend compilation passed in CI41319819062.

The first running-app candidate starts only after actual complete-build PASS.
It preserves old owned fixture state, creates fresh synthetic PG credentials,
and runs real dbt again with column and SUM-metric YAML; generated artifacts
are never edited. Frozen Garage2.5.0 serves real authenticated PUT/GET and a
wrong-secret403 before normal production migrations and backend startup.
Fresh populated runtime smokes cover numeric users, RO failures, RW paths,
authenticated PG/SCRAM, effective limits and loopback-only networking.

Runtime runs PG512MiB/.5CPU, Garage256MiB/.25CPU, app2560MiB/2.25CPU and
actor1024MiB/1CPU (4352MiB/4CPU total), with no build or fixture overlap.
The immutable launcher and existing full-object update verifier are reused;
all actual service objects are created while only PG runs and started through
an exact-ID/role/resource gate. No socket, published ports, external network,
privilege or source/API change is introduced. Fresh measured runtime peak
projection is15,718,926,905bytes including separate2GiB headroom; actual
30GB/df gates remain mandatory. Completed build copies are not reserved again.

The normal built CLI deploys authentic metadata using the honest known-version
setting `LIGHTDASH_DBT_VERSION=dbt-oss 2.0.8`; its warning/fallback remains visible.
A normal owner registers/creates the project and invites a distinct ordinary
member/editor. That editor must complete async metric queries350 and September300
with query UUIDs, generated SQL and actual rows. An owner-accessible private chart
must return403 to the editor, rather than401/404. Built frontend assets and normal
backend migrations/start are required. Whole catalog dump equality (only random
psql restrict-token lines normalized) and every Parquet hash must remain unchanged.
CI42493498053 started Garage, app and actor inside the reviewed profiles. Real
authenticated S3 PUT/GET and wrong-secret403 passed, then production migrations
passed. Backend startup stopped on missing `canvas@3.2.1` native output; actor
350/300/private403 remain UNRUN. Cleanup removed owned containers and secrets,
but capture correctly failed its raw-leak check on Garage's INFO access-key log.

The correction freezes the normal upstream Linux x64 N-API7 canvas prebuild in
`canvas-prebuild.json`: 30 exact files including 26 ELF objects, component license
families and upstream release/build-recipe provenance. Recipe versions do not
claim reproducible compilation; the exact libstdc++ build version is unknown.
No binaries are published. The existing frozen Node image supplies the listed
base sonames; their actual resolution remains an offline CI load/PNG check.
The normal pinned `prebuild-install -r napi --local-prebuilds` installs only that
verified archive offline in the app profile, without source-build fallback.
Every installed file hash/mode and a real canvas PNG are checked before S3,
migrations and app startup. Two archive copies and two full expansions add
66,016,906 bytes to the unchanged pre-runtime30GB/headroom/df gate.
Garage alone uses its supported `RUST_LOG` module filter to suppress the
`garage::server` and `garage_api_common::generic_server` INFO events containing
the key (creation, signed requests and403); warnings/errors and all raw
secret checks remain enabled. This corrected runtime candidate is UNRUN until
reviewed Ubuntu execution; this is
not a claim that app native initialization or the remaining12case matrix passed.


CI42496360017 passed the first complete app slice: ordinary350/September300,
private403, frontend/CLI, canvas and S3 controls, with catalog/Parquet invariance.
This is narrow evidence; the full12case matrix is not admitted.

The next necessary slice starts with a real OSS dbt view **before** full app
acquisition/build. `diagnostic.py --view-gate` uses a separate tiny project and
target directory over the existing orders table. Normal `build --select
orders_view --write-catalog` must produce genuine view metadata/results, followed
by fresh Python readback of persisted type VIEW,350/300 and catalogSpec1.1.
Authentic outputs (including failure outputs) are separately allowlisted; none
are edited. The initial peak adds258MiB: two sets of3x32MiB view artifacts,
two32MiB report copies and2MiB project/filesystem overhead. Original files and
exported copies are separately charged; the report is capped before writing,
with the same30GB/2GiB/df and runtime limits. Native support is still UNRUN; a
real rejection stops before app work, with no table substitution. The app's
later fresh fixture retains its original table-only diagnostic invocation.
Incremental, snapshots, docs and Python model exceptions remain UNRUN.

The same necessary app extension adds reviewer-derived fresh-session
403 -> share200/350 -> revoke403 with positive identity/query/owner controls,
and S3 correct/wrong/correct GET status+body-hash receipts on one existing object.
After all attempted owned container names and exact Garage IDs are verified
absent, exact canonical `work/garage` (SQLite/WAL/SHM included) is removed and
its absence recorded. Dedicated secrets0 remains a separate, narrower check;
this makes no secure-media-erasure or global-redaction claim. All new probes
and cleanup are UNRUN pending focused review and real Ubuntu execution.


CI42622459080 passed the persisted view and reviewer boundary probes; the
ordinary BI slice is supported. Remaining dbt assets are the next early gate,
not a promotion of the full matrix. A separate `assets` schema/project runs
seven normal stages before app acquisition: seed; incremental350; snapshot350;
update only inputid2 from200to210; incremental360; snapshot current360 with
retained old200/new210 history; real docs generation; and Python assessment.
Both incrementals must retain three unique keys and target table identity;
the second must take `is_incremental()` and retain actual DELETE+INSERT SQL,
with no full refresh or target replacement. Snapshot history must have four
versions, oldid2 closed and new210 current.

CI42629897369 passed both incremental readbacks and stable target identity,
but its information-schema query Parquet contained zero rows. The second
incremental now explicitly enables the pinned CLI's local OTEL JSONL sink at
file DEBUG level. Only completed successful `QueryExecuted` spans for that
model supply the SQL oracle; compiled SQL is insufficient. The full trace stays
unexported inside the existing128MiB stage bound. Its hash/size and selected
actual SQL enter the existing secret-checked report; console verbosity stays
unchanged. CI42632250861 captured the actual selected DELETE+INSERT successfully.

Normal `dbt docs generate` must produce the static HTML/assets and Parquet
index containing the actual model; catalog-only output is insufficient.
Browser/CDN execution is outside this artifact test. A dependency-free Python
model is actually attempted. Only its precise pinned DuckDB unsupported-job
error may be an explicit exception; unrelated setup/import/SQL errors fail.
No pandas or other model-execution dependency is acquired.

CI42632250861 observed the expected incremental, snapshot, docs and precise
Python-unsupported results, but the final full Parquet logical check failed:
DuckDB's TIMESTAMPTZ conversion requires optional `pytz`. The qualification-only
offline closure now pins pure-Python `pytz2026.6` (MIT, no dependencies) before
installation; its506321-byte wheel and5419008-byte allocated expansion add
16763345bytes under the existing compressed-plus-three-copies accounting.
The earlier no-new-package assumption is superseded for this observed decoder
need. No timestamp column/type or secret check is skipped or converted away.
Successful target decoding/export remains UNRUN until CI.

Each stage retains its own authentic target and command/exit/input receipts.
Selected native/docs Parquet is logically secret-checked before hash-bound
export; unmodified JSON retains the existing raw-secret/perfile guards. The
additional pre-install2GiB counts seven128MiB original target bounds and
separate exported subsets,64MiB source/logs,128MiB native data, two16MiB report
payloads and32MiB metadata reserve. User30GB, separate2GiB headroom and live df
gates remain mandatory. The native asset behaviors were observed in CI42632250861;
whole-group acceptance still needs successful decoding/export and independent
review. The app's fresh orders350 fixture and passing proofs stay unchanged.
Other matrix groups remain unimplemented/UNRUN.

The next early gate, `--publication-gate`, exercises scratch native primitives
for failed-check-no-publish and multi-table rollback; it is not an integrated
Dango publication protocol or a held BI query test. Existing native assets and
ordinary BI have passed CI42825753494 and their scoped independent reviews.

Two separate metadata schemas/data roots on the existing PG hold accepted and
candidate lakes, leaving the original app lake unchanged. Real candidate dbt
models build360/8/360. A deliberate singular-test failure must be exercised
through the promotion entrypoint and refused before accepted writes. Missing
and stale receipts are refused too. A separately passing check must match the
current fixture/input/output hashes, exact resources and authentic artifacts.

Native partial sales replacement reaches360/7/350/r0, then an actual missing
relation error triggers ROLLBACK. A fresh single-query reader must retain
350/7/350/r0 and all rows/snapshot IDs. One subsequent transaction copies all
three tables and updates the marker; fresh readers see360/8/360/r1 with exact
rows, stable table IDs and one additional committed snapshot. A direct engine
READ_ONLY UPDATE must fail under the RW fixture mount and leave that state
unchanged. This is distinct from the earlier Lightdash validator refusal.

The extra640MiB accounts original targets256MiB, exported copies192MiB,
project/logs32MiB, original/exported report32MiB, new native data/PG metadata64MiB
and filesystem residual64MiB. Actual caps,30GB/separate2GiB headroom and df
checks remain mandatory. Only the report and two real dbt JSON sets are added
to the secret-checked allowlist. New cases4/7 remain native UNRUN until CI.

CI42835740607 passed the reviewed scratch failed-check/transaction primitives;
four native groups are supported. The next early `--namespaces-gate` checks
actual Alice/Bob catalog independence, not same-branch labels or user security.
Both retain exact `sales.orders` and `finance.orders` in distinct PG metadata
schemas and canonical data roots. Actual namespace OIDs, disjoint table UUIDs
and catalog-bound own-root Parquet paths/sizes/hashes establish those bindings.
Local integer IDs may overlap and equal initial content hashes are allowed.

Both actors start350/finance7. Bob's real commit doubles its sales to700 while
Alice's rows/snapshots stay350/7. Alice then commits sales360/finance8 while Bob
stays700/7. Only after those observations pass does a labelled Alice-only revert
restore350/7; final Bob remains700/7. Fresh READ_ONLY connections check complete
rows/keys/names and committed snapshots at each phase; original350/September300
and its public snapshot history stay unchanged. Same UID/PG user is explicit:
there is no hostile-tenant, Dango workspace, dlt or held BI reader claim.

Only the safe `native-namespaces.json` report is added to evidence. The extra
128MiB counts64MiB actor data/PG metadata,32MiB original/exported report and32MiB
source/filesystem reserve before acquisition/install. Actual allocation caps,
30GB/separate2GiBheadroom/df and unchanged profiles still apply. Case2 remains
native UNRUN until reviewed CI; no new dependency/service/profile is introduced.
