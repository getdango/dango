"""dango/telemetry.py

Opt-in anonymous telemetry: a single "install ping" fired once at
`dango init`. No heartbeat, no scheduler hook — that is future scope.

Payload is limited to an anonymous install UUID, Dango version, Python
version, OS name, and source *type* names (e.g. "postgres", "stripe") —
never source names, credentials, row counts, schema, or query text.

Fire-and-forget: 2s timeout, silent on ALL failure (network, disk,
permission, whatever) — telemetry must never raise, never block, and
never slow down or break any CLI command. Failed pings are never queued
for retry; a dropped ping is just dropped.

Identity is machine-level (``~/.dango/telemetry.json``), not project-scoped,
so one consultant with five client projects on one laptop counts as one
user rather than five.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any
from uuid import uuid4

TELEMETRY_ENDPOINT = "https://telemetry.getdango.dev/v1/ping"

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


def is_ci() -> bool:
    """Detect whether the process is running inside a known CI environment.

    CI is the single biggest source of install-count inflation
    (``LAUNCH-READINESS.md`` §A1c), so pings must never fire from CI
    regardless of any stored consent.

    Returns:
        True if any recognized CI environment variable is set.
    """
    return any(os.environ.get(var) for var in _CI_ENV_VARS)


def is_telemetry_enabled() -> bool:
    """Check whether telemetry is enabled, honouring every opt-out signal.

    Any one of the following disables telemetry, checked in this order:
    ``DO_NOT_TRACK=1``, ``DANGO_TELEMETRY=0``, running in CI, ``telemetry:
    false`` in ``~/.dango/config.yml``, or a previously stored "no" answer
    in ``~/.dango/telemetry.json``. A missing or corrupt config/identity
    file is treated as "no opt-out recorded" rather than raising.

    Returns:
        True if telemetry may be sent, False if any opt-out applies.
    """
    if os.environ.get("DO_NOT_TRACK") == "1":
        return False
    if os.environ.get("DANGO_TELEMETRY") == "0":
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
    """Fire an anonymous telemetry ping. Never raises, never blocks > 2s.

    A no-op when telemetry is disabled (see `is_telemetry_enabled`). All
    failures — DNS, timeout, TLS, a non-2xx response, anything — are
    swallowed silently and never queued for retry.

    Args:
        event: The event name, e.g. ``"install"``.
        source_types: Configured source *type* names only (e.g.
            ``["postgres", "stripe"]``) — never source names, credentials,
            or schema content.
    """
    if not is_telemetry_enabled():
        return

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
            "is_ci": False,
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
