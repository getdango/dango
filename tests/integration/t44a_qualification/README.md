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

AI assistance and authority: Codex authored this scratch qualification recipe under
the human-approved T44A batch. Independent review and coordinator verification are
required before publication/execution; no merge or product change is authorized.
