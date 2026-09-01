"""tests/unit/test_egress_allowlist.py

Informational CI check for LAUNCH-READINESS.md §A5: detects hosts Dango's own
Python process contacts that aren't documented in docs/network-egress.yml.
Not a merge-blocking gate — the `egress` job is deliberately not in required
status checks (see .github/workflows/ci.yml) — but it does fail loudly and
visibly on an undisclosed host, which is the detection mechanism §A5 asks for.
"""

from __future__ import annotations

import inspect
import socket
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
EGRESS_DOC = REPO_ROOT / "docs" / "network-egress.yml"

ALWAYS_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _load_egress_doc() -> dict[str, Any]:
    return yaml.safe_load(EGRESS_DOC.read_text())


def _normalize_host(host: str) -> str:
    """Lowercase and strip a trailing root '.' so 'Host.example.com.' and
    'host.example.com' compare equal — DNS is case-insensitive and a
    resolver may present either form."""
    return host.lower().rstrip(".")


def _documented_hosts() -> set[str]:
    data = _load_egress_doc()
    hosts: set[str] = set()
    for section in ("telemetry", "functional"):
        for i, entry in enumerate(data.get(section, [])):
            host = entry.get("host")
            assert host, (
                f"docs/network-egress.yml: entry {i} in '{section}' is missing "
                f"a 'host' key: {entry!r}"
            )
            hosts.add(host)
    return {_normalize_host(h) for h in hosts}


class _AllowlistGuard:
    """Monkeypatches socket.getaddrinfo to block DNS resolution of any hostname
    not in *allowed_hosts*, and records what it blocked.

    getaddrinfo (not socket.connect) is the patch point: it still has the
    original hostname string before resolution to an IP. requests, urllib3,
    urllib.request, and httpx all route through it for hostname lookups.

    Blocked hosts are recorded on self.blocked_hosts rather than relied upon
    via a raised exception reaching the test: dango/utils/post_sync.py's
    dispatch_post_sync_hooks (lines 378-387) wraps each hook in a bare
    ``except Exception`` and only logs a hook name, so an exception raised
    from inside a hook (e.g. the PII-scan hook attempting a blocked host)
    never propagates to the caller. Asserting on self.blocked_hosts after
    the `with` block survives that swallowing.

    Caveat: a C extension doing its own libc DNS resolution (bypassing
    Python's socket module) would not be caught. Not a concern for the
    workflow this test drives — no DuckDB extensions (e.g. httpfs) load.
    """

    def __init__(self, allowed_hosts: set[str]) -> None:
        # allowed_hosts (from _documented_hosts) is already normalized;
        # ALWAYS_ALLOWED_HOSTS doesn't need it (no case/dot variation).
        self.allowed_hosts = allowed_hosts | ALWAYS_ALLOWED_HOSTS
        self.blocked_hosts: set[str] = set()
        self._original = socket.getaddrinfo

    def __enter__(self) -> _AllowlistGuard:
        socket.getaddrinfo = self._patched  # type: ignore[assignment]
        return self

    def __exit__(self, *exc_info: object) -> None:
        socket.getaddrinfo = self._original  # type: ignore[assignment]

    def _patched(self, host: str | None, *args: Any, **kwargs: Any) -> Any:
        if host is None:
            return self._original(host, *args, **kwargs)
        normalized = _normalize_host(host)
        if normalized not in self.allowed_hosts:
            # Store the normalized form: this is what gets surfaced in the
            # final assertion message, and it should match exactly what
            # someone would type into docs/network-egress.yml, not a
            # case/trailing-dot variant that looks like a different host.
            self.blocked_hosts.add(normalized)
            raise socket.gaierror(
                socket.EAI_NONAME,
                f"blocked by egress allowlist test: {host!r} (normalized: {normalized!r})",
            )
        return self._original(host, *args, **kwargs)


@pytest.mark.unit
class TestNetworkEgressDoc:
    """Doc-consistency and real-introspection checks — no network, fast."""

    def test_network_egress_yml_exists(self) -> None:
        assert EGRESS_DOC.exists(), "docs/network-egress.yml not found"
        data = _load_egress_doc()
        assert "telemetry" in data
        assert "functional" in data

    def test_network_egress_yml_has_all_known_providers(self) -> None:
        data = _load_egress_doc()
        providers: set[str] = set()
        for i, entry in enumerate(data["telemetry"]):
            provider = entry.get("provider")
            assert provider, (
                f"docs/network-egress.yml: telemetry entry {i} is missing a "
                f"'provider' key: {entry!r}"
            )
            providers.add(provider)
        assert providers == {"dango", "dbt-core", "dlt", "metabase"}

    def test_dlt_runtime_configuration_has_telemetry_field(self) -> None:
        """Real introspection against the installed dlt version, not a
        hardcoded string comparison. Catches a silent rename of the field
        docs/network-egress.yml claims controls dlt's telemetry (this is
        exactly the risk LAUNCH-READINESS.md §A2 calls out: "If dlt renames
        dlthub_telemetry... a silent failure means you are telling users
        something false")."""
        from dlt.common.configuration.specs.runtime_configuration import (
            RuntimeConfiguration,
        )

        fields = RuntimeConfiguration.__dataclass_fields__
        assert "dlthub_telemetry" in fields, (
            "dlt renamed its telemetry opt-out field — update "
            "docs/network-egress.yml and the write-through in the telemetry command"
        )

    def test_dbt_send_anonymous_usage_stats_envvar_name(self) -> None:
        """Real check against the installed dbt-core source, not memory.
        Catches a silent rename of the env var docs/network-egress.yml
        documents as dbt's telemetry opt-out control."""
        from dbt.cli import params as dbt_params

        source = inspect.getsource(dbt_params)
        assert "DBT_SEND_ANONYMOUS_USAGE_STATS" in source, (
            "dbt-core's telemetry opt-out env var name changed — update "
            "docs/network-egress.yml and the write-through in the telemetry command"
        )


@pytest.mark.unit
@pytest.mark.egress
class TestLiveEgressDetection:
    """Live network-detection test — its own class because it carries the
    extra `egress` marker the doc-consistency tests above don't need
    (STANDARDS.md §7: markers apply at the class level)."""

    def test_dango_init_source_sync_only_contacts_allowlisted_hosts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Runs a real dango init (--skip-wizard) -> CSV source -> sync
        workflow with DNS resolution blocked for any host outside
        docs/network-egress.yml. This is detection, not documentation: it
        fails if an undisclosed host is contacted, it does not just check
        the doc is self-consistent.

        Scope: catches egress from Dango's own Python process only.
        - dbt runs as a real subprocess during dango init (`dbt docs
          generate`, cli/init.py:1153-1225, unconditional — dbt docs
          generation is treated as CRITICAL and rolls back init on failure)
          and inside run_sync when skip_dbt=False — its own DNS/socket calls
          happen in a separate process and are invisible to this
          monkeypatch. skip_dbt=True below avoids also running it during
          sync, since running it here adds latency for zero detection
          coverage. dbt-core's anonymous usage stats are on by default
          (dbt/tracking.py) and, being a real subprocess call this
          monkeypatch can't see, would otherwise fire a real ping to
          fishtownanalytics.sinter-collect.com on every run of this test via
          the init step above — DBT_SEND_ANONYMOUS_USAGE_STATS/DO_NOT_TRACK
          below disable that. This is an env var, not subprocess network
          interception (still out of scope) — it doesn't let this test see
          dbt's egress, it just stops the init step from producing an actual
          telemetry send as an unintended side effect of writing a
          telemetry-detection test.
        - dlt itself runs in-process (imported directly in dlt_runner.py,
          not subprocess'd) — its DNS calls happen on this thread and are
          visible to _AllowlistGuard, unlike dbt's. But dlt's own telemetry
          is dispatched fire-and-forget on a background thread pool
          (dlt/common/runtime/anon_tracker.py: `_THREAD_POOL.thread_pool.
          submit(_future_send)`, never joined by the caller) — a send queued
          there could execute after this `with` block exits and
          _AllowlistGuard restores the real socket.getaddrinfo, racing past
          detection. `stop_telemetry()` below forces a synchronous flush
          (`disable_anon_tracker()` -> `ManagedThreadPool.stop(wait=True)`,
          which blocks on `ThreadPoolExecutor.shutdown(wait=True)`) so any
          queued send is forced to happen, and be checked against the
          allowlist, deterministically inside the guard.
          Verified empirically for the CSV-source path this test drives:
          `dlt.common.runtime.telemetry.is_telemetry_started()` and
          `dlt.common.runtime.anon_tracker._ANON_TRACKER_ENDPOINT` both stay
          unset through a full run_sync() call here — dlt's runtime-config
          lazy-init (dlt/common/runtime/run_context.py:
          `RunContext.runtime_config` property) is apparently never touched
          by this code path, so there is currently no live telemetry event
          for stop_telemetry() to flush. It's kept anyway as defense in
          depth — a different source type, or a future dlt/dango version,
          could touch that property and start telemetry, and this test
          shouldn't silently stop covering dlt the day that happens. Do not
          read "dlt IS covered" as "dlt telemetry was observed firing here";
          it wasn't, in this specific run.
        - `dango start`/`dango serve` are excluded entirely: they require a
          running Docker daemon to launch Metabase (cli/commands/platform.py:
          464-495), which would make this job slow and Docker-availability
          flaky for no additional Python-process detection coverage — their
          egress (Metabase container, localhost health polling) is
          separately documented/functional.

        Marked `egress` (in addition to `unit`) and excluded from the
        general `test` CI job matrix (`-m "not egress"`,
        .github/workflows/ci.yml) — that job's 3-way Python-version matrix
        doesn't run the `egress` job's spaCy pre-install step, so without
        this exclusion the test would fall through to
        governance/pii_detector.py's live model-download path three times
        per CI run, adding real network dependency to the *required* Test
        (Python 3.10)/(3.12) checks for no detection benefit (the dedicated
        `egress` job already covers this test hermetically).

        Local reproduction: run `python -m spacy download en_core_web_sm`
        once first (matches the CI `egress` job's pre-install step) — the
        post-sync PII-scan hook isn't gated by skip_dbt (post_sync.py:381,
        unconditional), so without the model already installed this test
        falls through to governance/pii_detector.py's live download path
        instead of a clean skip.
        """
        from dlt.common.runtime.telemetry import stop_telemetry

        from dango.cli.init import init_project
        from dango.config.models import SourceType
        from dango.ingestion import run_sync
        from dango.utils.driver import METABASE_DUCKDB_DRIVER_VERSION
        from tests.factories.config_factories import (
            make_csv_source_config,
            make_data_source,
        )

        monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
        monkeypatch.setenv("DO_NOT_TRACK", "1")

        # DO_NOT_TRACK also now does double duty: cli/init.py's
        # _prompt_telemetry_consent() (Dango's own opt-in telemetry, added
        # after this test was first written) is called unconditionally
        # inside initialize() -- not gated by skip_wizard -- and would
        # otherwise call click.prompt() interactively. is_ci() short-circuits
        # it on real CI (GITHUB_ACTIONS=true), but a contributor running this
        # test locally, on a machine that has never run a real `dango init`
        # (so has_recorded_consent() is False), would hit a live interactive
        # prompt without this. is_telemetry_enabled() (dango/telemetry.py)
        # honors DO_NOT_TRACK the same way dbt does, so the env var above
        # already covers this -- confirmed empirically (no hang, no prompt)
        # after dbt-core 1.11/dango-telemetry landed on v1.0.8 post-branch.

        # Pre-seed the Metabase DuckDB driver so init's download step
        # (cli/init.py:_setup_metabase, utils/driver.py:driver_needs_update)
        # is a no-op. This test asserts against *undocumented* hosts; it
        # does not verify the documented github.com download path is
        # reachable — depending on live GitHub Releases availability would
        # make an informational CI job flaky for zero detection benefit.
        plugins_dir = tmp_path / "metabase-plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        (plugins_dir / "duckdb.metabase-driver.jar").write_bytes(b"test-driver-stub")
        (plugins_dir / ".driver-version").write_text(METABASE_DUCKDB_DRIVER_VERSION + "\n")

        allowed = _documented_hosts()
        guard = _AllowlistGuard(allowed)

        with guard:
            init_project(tmp_path, skip_wizard=True)

            data_dir = tmp_path / "data" / "test_csv"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "test.csv").write_text("id,value\n1,a\n2,b\n")

            source = make_data_source(
                SourceType.CSV,
                name="test_csv",
                csv=make_csv_source_config(directory=str(data_dir)),
            )

            result = run_sync(tmp_path, [source], skip_dbt=True)

            # Force dlt's background telemetry thread pool to drain before
            # the guard exits — see the docstring above. Safe to call even
            # if no telemetry was actually queued (no-op then).
            stop_telemetry()

        assert guard.blocked_hosts == set(), (
            f"Undocumented host(s) contacted during dango init/source/sync: "
            f"{guard.blocked_hosts}. If this is expected, add it to "
            f"docs/network-egress.yml (telemetry or functional section) with "
            f"an honest reason. Do not loosen this test to make it pass."
        )
        assert result["failed_sources"] == [], f"sync failed: {result['failed_sources']}"
