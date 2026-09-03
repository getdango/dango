"""dango/telemetry.py

Opt-in anonymous telemetry: a one-time "install ping" fired at `dango
init`, plus a weekly "heartbeat" ping registered as a fixed internal
scheduler job (`SchedulerService._setup_telemetry_heartbeat()` in
`platform/scheduling/scheduler.py`) whenever `dango start` runs. The
heartbeat exists so an "active install" (>=2 heartbeats, >=7 days
apart) is measurable — the install ping alone can't distinguish a
one-time `dango init` from an install still in active use weeks later.

Payload is limited to an anonymous install UUID, Dango version, Python
version, OS name, and source *type* names (e.g. "postgres", "stripe") —
never source names, credentials, row counts, schema, or query text.

Best-effort with a bounded wait: the network call runs on a daemon
thread, joined with a timeout of `_TIMEOUT_SECONDS`, so `dango init`
waits at most that long for the ping to actually land before moving on
— a short-lived CLI process exits so quickly that an un-joined thread
essentially never gets scheduled before interpreter shutdown kills it
(verified empirically: without a join, the ping never completes on a
normal `dango init` exit). Silent on ALL failure (network, disk,
permission, whatever) — telemetry must never raise or break any CLI
command, and never waits longer than the timeout regardless of what's
slow (DNS, connect, read, anything). Failed or unfinished pings are
never queued for retry; a dropped ping is just dropped.

Identity is machine-level (``~/.dango/telemetry.json``), not project-scoped,
so one consultant with five client projects on one laptop counts as one
user rather than five.
"""

from __future__ import annotations

import json
import os
import platform
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

TELEMETRY_ENDPOINT = "https://telemetry.getdango.dev/v1/ping"

# The four telemetry providers `dango telemetry` and the `/settings/telemetry`
# web page both control. Defined here (Level 0) rather than in
# `cli/commands/telemetry.py` (Level 3) so `web/routes/telemetry.py` (Level 2)
# can import it without a Level-2-imports-Level-3 violation.
PROVIDERS = ("dango", "dbt", "dlt", "metabase")

_TIMEOUT_SECONDS = 2

_CONFIG_DIR = Path.home() / ".dango"
_IDENTITY_FILE = _CONFIG_DIR / "telemetry.json"
_GLOBAL_CONFIG_FILE = _CONFIG_DIR / "config.yml"

_CI_ENV_VARS = (
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "JENKINS_URL",
    "BUILDKITE",
    "CIRCLECI",
    "TRAVIS",
    "CODEBUILD_BUILD_ID",
)

# Matches the community DO_NOT_TRACK convention (consoledonottrack.com) and
# dbt-core's own DO_NOT_TRACK check (dbt/cli/flags.py) — the exact tool this
# module's opt-out is meant to be consistent with.
_TRUTHY_ENV_VALUES = ("1", "t", "true", "y", "yes")
_FALSY_ENV_VALUES = ("0", "f", "false", "n", "no")


def is_ci() -> bool:
    """Detect whether the process is running inside a known CI environment.

    CI is the single biggest source of install-count inflation
    (``LAUNCH-READINESS.md`` §A1c), so pings must never fire from CI
    regardless of any stored consent.

    A variable set to an explicit falsy value (e.g. ``CI=false``, as some
    nested build tools set) is not treated as CI, even though it is
    "present" — only bare presence with a non-falsy value counts.

    Returns:
        True if any recognized CI environment variable indicates CI.
    """
    for var in _CI_ENV_VARS:
        value = os.environ.get(var)
        if value and value.strip().lower() not in _FALSY_ENV_VALUES:
            return True
    return False


def is_telemetry_enabled() -> bool:
    """Check whether telemetry is enabled, honouring every opt-out signal.

    Any one of the following disables telemetry, checked in this order:
    ``DO_NOT_TRACK`` set to a truthy value, ``DANGO_TELEMETRY`` set to a
    falsy value, running in CI, ``telemetry: false`` in
    ``~/.dango/config.yml``, or a previously stored "no" answer in
    ``~/.dango/telemetry.json``. A missing or corrupt config/identity file
    is treated as "no opt-out recorded" rather than raising.

    Returns:
        True if telemetry may be sent, False if any opt-out applies.
    """
    if os.environ.get("DO_NOT_TRACK", "").strip().lower() in _TRUTHY_ENV_VALUES:
        return False
    if os.environ.get("DANGO_TELEMETRY", "").strip().lower() in _FALSY_ENV_VALUES:
        return False
    if is_ci():
        return False

    if _GLOBAL_CONFIG_FILE.is_file():
        try:
            import yaml

            with open(_GLOBAL_CONFIG_FILE) as f:
                config_data: dict[str, Any] = yaml.safe_load(f) or {}
            if config_data.get("telemetry") is False:
                return False
        except Exception:
            pass

    if _IDENTITY_FILE.is_file():
        try:
            with open(_IDENTITY_FILE) as f:
                identity_data: dict[str, Any] = json.load(f)
            if identity_data.get("enabled") is False:
                return False
        except Exception:
            pass

    return True


def _read_global_config() -> dict[str, Any]:
    """Read ~/.dango/config.yml, returning {} if absent, unreadable, or malformed."""
    if not _GLOBAL_CONFIG_FILE.is_file():
        return {}
    try:
        import yaml

        with open(_GLOBAL_CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _write_global_config_key(key: str, value: Any) -> bool:
    """Set a single key in ~/.dango/config.yml, preserving every other key.

    Never raises — matching this module's existing telemetry-write
    conventions (set_telemetry_enabled). Instead, returns whether the write
    succeeded so callers that need an honest failure signal (e.g.
    `_set_dbt_telemetry()`, which converts a False return into a
    click.ClickException so `--all` can skip and continue) can act on it,
    while a caller with a genuinely best-effort contract can ignore the
    return value.

    Returns:
        True if the key was written to disk, False if the write failed for
        any reason (the setting doesn't stick and can be retried).
    """
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        import yaml

        data = _read_global_config()
        data[key] = value
        with open(_GLOBAL_CONFIG_FILE, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        return True
    except Exception:
        return False


def get_dbt_telemetry_state() -> bool:
    """Return dbt's current opt-in state per ~/.dango/config.yml (default: on).

    Relocated here (Level 0) from `cli/commands/telemetry.py`'s
    `_get_dbt_telemetry_state()` (1.0.8-U) — both the CLI and
    `web/routes/telemetry.py` (Level 2) call this same function so there is
    one real implementation, not two.
    """
    data = _read_global_config()
    return bool(data.get("dbt_telemetry", True))


def set_dbt_telemetry_state(enabled: bool) -> None:
    """Write dbt's opt-out state to ~/.dango/config.yml under the dbt_telemetry key.

    Machine-level (~/.dango/), matching Dango's own telemetry identity scope
    — one opt-out covers every project on the machine. Read by
    `_dbt_telemetry_env()` in `transformation/__init__.py`.

    Relocated here (Level 0) from `cli/commands/telemetry.py`'s
    `_set_dbt_telemetry()` (1.0.8-U). This module has no `click` import
    (Level 0 cannot depend on Level 3), so callers that need a
    `click.ClickException` (the CLI) catch the plain `OSError` below and
    wrap it themselves.

    Raises:
        OSError: If the write fails (e.g. permission denied, disk full) —
            converted from `_write_global_config_key()`'s False return so
            callers get an honest failure signal to act on.
    """
    if not _write_global_config_key("dbt_telemetry", enabled):
        raise OSError("Could not write ~/.dango/config.yml")


def get_dlt_telemetry_state(project_root: Path | None) -> bool:
    """Return dlt's current opt-in state per .dlt/config.toml (default: on).

    Relocated here (Level 0) from `cli/commands/telemetry.py`'s
    `_get_dlt_telemetry_state()` (1.0.8-U) — only touches `.dlt/config.toml`
    via `tomlkit` (third-party), no dango-internal import, so this has
    always belonged at Level 0.
    """
    if project_root is None:
        return True
    config_path = project_root / ".dlt" / "config.toml"
    if not config_path.exists():
        return True
    try:
        import tomlkit

        doc = tomlkit.parse(config_path.read_text())
        val = doc.get("runtime", {}).get("dlthub_telemetry", True)
        return bool(val)
    except Exception:
        return True


def set_dlt_telemetry_state(project_root: Path, enabled: bool) -> None:
    """Write dlthub_telemetry to .dlt/config.toml under [runtime].

    Writes a native TOML boolean (not a string) to match the value type
    dlt's own `dlt telemetry switch` CLI writes —
    RuntimeConfiguration.dlthub_telemetry is a bool-typed field.

    Relocated here (Level 0), moved verbatim from
    `cli/commands/telemetry.py`'s `_set_dlt_telemetry()` body (1.0.8-U),
    minus the `click.ClickException` wrapping — this module has no `click`
    import (Level 0 cannot depend on Level 3). Callers that need a
    `click.ClickException` (the CLI) catch the exceptions below and wrap
    them with the same message text as before the relocation.

    Raises:
        OSError: If the file can't be read/written (e.g. permissions).
        ValueError: Wrapping `tomlkit.exceptions.TOMLKitError` if the
            existing `.dlt/config.toml` is malformed (e.g. from manual
            editing).
    """
    import tomlkit
    from tomlkit.exceptions import TOMLKitError

    config_path = project_root / ".dlt" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc = tomlkit.parse(config_path.read_text()) if config_path.exists() else tomlkit.document()
    except TOMLKitError as e:
        raise ValueError(str(e)) from e
    if "runtime" not in doc:
        doc.add("runtime", tomlkit.table())
    doc["runtime"]["dlthub_telemetry"] = enabled
    config_path.write_text(tomlkit.dumps(doc))


def has_recorded_consent() -> bool:
    """Check whether the user has already answered the telemetry prompt.

    Returns:
        True if ``~/.dango/telemetry.json`` already stores an explicit
        "enabled" answer (from a prior `dango init` run), False otherwise.
    """
    try:
        if _IDENTITY_FILE.is_file():
            with open(_IDENTITY_FILE) as f:
                data: dict[str, Any] = json.load(f)
            return "enabled" in data
    except Exception:
        pass
    return False


def _get_or_create_uuid() -> str:
    """Return the persisted anonymous install UUID, creating it if absent.

    Does a plain read-modify-write with no file lock: this only ever runs
    once, synchronously, inside `dango init` (never from a background or
    scheduled process), matching the no-lock precedent already used for
    ``~/.dango/`` in `dango.config.cloud_credentials` and
    `dango.platform.local.network`.
    """
    try:
        if _IDENTITY_FILE.is_file():
            with open(_IDENTITY_FILE) as f:
                data: dict[str, Any] = json.load(f)
            existing = data.get("uuid")
            if existing:
                return str(existing)
    except Exception:
        pass

    new_uuid = str(uuid4())
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        existing_data: dict[str, Any] = {}
        if _IDENTITY_FILE.is_file():
            try:
                with open(_IDENTITY_FILE) as f:
                    existing_data = json.load(f)
            except Exception:
                existing_data = {}
        existing_data["uuid"] = new_uuid
        with open(_IDENTITY_FILE, "w") as f:
            json.dump(existing_data, f, indent=2)
    except Exception:
        pass
    return new_uuid


def set_telemetry_enabled(enabled: bool) -> None:
    """Persist the user's telemetry consent answer.

    Called once, right after the `dango init` prompt. Merges with (does
    not clobber) any existing ``uuid`` key already stored — a plain
    read-modify-write, same single-process reasoning as
    `_get_or_create_uuid`. Never raises: a failure to persist just means
    the prompt reappears on the next `dango init`.

    Args:
        enabled: The user's answer — True for "yes", False for "no".
    """
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if _IDENTITY_FILE.is_file():
            try:
                with open(_IDENTITY_FILE) as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data["enabled"] = enabled
        with open(_IDENTITY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def ping(event: str, source_types: list[str] | None = None) -> None:
    """Fire an anonymous telemetry ping. Never raises, waits at most `_TIMEOUT_SECONDS`.

    A no-op when telemetry is disabled (see `is_telemetry_enabled`). The
    actual network call runs on a daemon thread, joined with a timeout —
    a short-lived CLI process exits so quickly that an un-joined
    background thread essentially never gets a chance to run before
    interpreter shutdown kills it (verified empirically), so a bounded
    wait here is what actually gives the ping a real chance to land, not
    just a theoretical one. Never waits longer than the timeout even if
    the thread is still running (DNS hang, slow network, anything) — it
    is abandoned as a daemon thread at that point, and the caller
    proceeds immediately. All failures — DNS, timeout, TLS, a non-2xx
    response, anything — are swallowed silently inside that thread and
    never queued for retry.

    Args:
        event: The event name, e.g. ``"install"``.
        source_types: Configured source *type* names only (e.g.
            ``["postgres", "stripe"]``) — never source names, credentials,
            or schema content.
    """
    if not is_telemetry_enabled():
        return

    thread = threading.Thread(target=_send_ping, args=(event, source_types), daemon=True)
    thread.start()
    thread.join(timeout=_TIMEOUT_SECONDS)


def _send_ping(event: str, source_types: list[str] | None) -> None:
    """Build and POST the ping payload. Runs on a background thread; never raises."""
    try:
        import urllib.request

        from dango import __version__

        payload = {
            "uuid": _get_or_create_uuid(),
            "event": event,
            "version": __version__,
            "os": platform.system(),
            "python_version": platform.python_version(),
            "source_types": sorted(set(source_types or [])),
            # No is_ci field: ping() already refuses to run at all when
            # is_ci() is true, so every payload that actually reaches here
            # would always carry the same fixed False — sending it would
            # imply per-ping variability that can never exist client-side.
            # The Worker's schema defaults this column to 0 when absent.
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            TELEMETRY_ENDPOINT,
            data=body,
            headers={
                "Content-Type": "application/json",
                # Cloudflare's bot-fight mode (enabled on this endpoint) blocks
                # requests with no User-Agent — urllib sends none by default.
                "User-Agent": f"dango-cli/{__version__}",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS)
    except Exception:
        pass


def heartbeat(source_types: list[str] | None = None) -> None:
    """Fire an anonymous ``"heartbeat"`` telemetry ping.

    A thin wrapper around `ping()` — reuses its threading/timeout/opt-out
    logic unchanged. Called weekly by `heartbeat_job()` via the scheduler's
    fixed internal ``dango-internal:telemetry-heartbeat`` job (see
    `SchedulerService._setup_telemetry_heartbeat()`), never directly by
    user-facing code.

    Args:
        source_types: Configured source *type* names only (e.g.
            ``["postgres", "stripe"]``) — never source names, credentials,
            or schema content.
    """
    ping("heartbeat", source_types=source_types)


def heartbeat_job(project_root: str) -> None:
    """Module-level, picklable wrapper for the scheduled heartbeat job.

    APScheduler 3.x requires module-level functions for job persistence
    (matching `cleanup_history_job`/`cleanup_login_attempts_job`'s
    convention of taking `project_root` as a plain string, not a `Path` —
    APScheduler's `SQLAlchemyJobStore` needs args to be picklable).

    Reads configured source *type* names the same way
    `_prompt_telemetry_consent()` does in `cli/init.py`
    (``config.sources.get_enabled_sources()`` -> ``.type.value``). A
    config load failure still lets the heartbeat fire (with no
    ``source_types``) rather than skip it entirely — matching this
    module's existing "telemetry must never break, and never blocks on a
    detail it doesn't strictly need" posture.

    Args:
        project_root: Dango project root as a string (APScheduler
            serializes args).
    """
    source_types: list[str] | None = None
    try:
        from dango.config.helpers import get_config

        config = get_config(Path(project_root))
        source_types = [s.type.value for s in config.sources.get_enabled_sources()]
    except Exception:
        pass

    heartbeat(source_types=source_types)
