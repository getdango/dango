"""dango/platform/cloud/isolation_status.py

What a cloud server says about script and notebook isolation, for ``remote status`` and
``dango remote isolation status``.

One root probe reads the two root-owned markers (same validity test as
:mod:`dango.platform.run_as`), asks the firewall live and reads the non-secret version record;
the client parses the output as data.  Collection never raises: anything unreadable is ``unknown``.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

from rich.markup import escape

_RECORD_MAX_BYTES = 4096
_INFO_MAX_BYTES = 512
_FIELD_MAX = 120


# One call, run as root, reading root-owned things only.  Both markers use the test that
# ``dango.platform.run_as`` applies (regular file, uid 0, not group/other-writable, no symlink,
# inside root-owned closed ``/srv/dango/runner`` and ``/srv/dango``);
# ``find -maxdepth 0`` without ``-L`` never follows a link.  The firewall table is asked live.
# The version record is read like the Docker access probe and parsed by the client as data; the
# installed version is only a directory name (its characters are restricted, nothing is executed).
ISOLATION_PROBE_CMD = (
    "ROOT=0; S=/srv/dango; R=$S/runner; V=$S/server-version.json; "
    'safe() { [ -n "$(find "$2" -maxdepth 0 -type "$1" -uid "$ROOT" ! -perm /022 '
    '2>/dev/null)" ]; }; '
    'if safe d "$S" && safe d "$R" && safe f "$R/active"; then echo active=1; '
    "else echo active=0; fi; "
    'if safe d "$S" && safe d "$R" && safe f "$R/disabled"; then echo disabled=1; '
    "else echo disabled=0; fi; "
    'if safe d "$S" && safe d "$R" && safe f "$R/disabled"; then printf disabled_info=; '
    f"timeout 5 head -c {_INFO_MAX_BYTES} \"$R/disabled\" 2>/dev/null | tr -d '\\000-\\037'; "
    "echo; fi; "
    "if /usr/sbin/nft list table inet dango_runner >/dev/null 2>&1; "
    "then echo nft=1; else echo nft=0; fi; "
    "n=0; for u in dango-run dango-nb; do "
    'getent passwd "$u" >/dev/null 2>&1 && n=$((n+1)); done; echo users=$n; '
    "if [ -f /etc/sudoers.d/dango-runner ]; then echo sudoers=1; else echo sudoers=0; fi; "
    # the installed version is read from the venv's metadata directory name, never by running it
    "d=$(ls -d /srv/dango/venv/lib/python3*/site-packages/getdango-*.dist-info 2>/dev/null "
    "| head -n 1); d=${d##*/getdango-}; d=${d%.dist-info}; "
    "echo installed=$(printf %s \"$d\" | tr -cd '0-9A-Za-z.+!_-'); "
    "echo ---; "
    f'if safe d "$S" && safe f "$V"; then timeout 5 head -c {_RECORD_MAX_BYTES} "$V" '
    "2>/dev/null; fi; true"
)


@dataclass(frozen=True)
class IsolationStatus:
    """What the server says about script and notebook isolation (no secrets)."""

    #: ``on``, ``disabled``, ``missing``, ``unsupported`` (the installed Dango predates isolation)
    #: or ``unknown``
    state: str = "unknown"
    #: ``restricted``, ``unrestricted`` or ``unknown``
    loopback: str = "unknown"
    #: loopback value in the version record, ``unknown`` when the key is absent
    record: str = "unknown"
    active_marker: bool | None = None
    disabled_marker: bool | None = None
    users: str | None = None
    sudoers: bool | None = None
    nft: bool | None = None
    #: who and when, from the opt-out marker (informational, cleaned and bounded)
    disabled_by: str | None = None
    disabled_at: str | None = None
    #: Dango version installed on the server, from the venv metadata (None when unreadable)
    installed: str | None = None


def _clean(value: object) -> str | None:
    """A bounded printable string from marker content, or None."""
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if ch.isprintable())[:_FIELD_MAX].strip()
    return text or None


def _opt_out_info(raw: str) -> tuple[str | None, str | None]:
    """``(by, at)`` from the marker's content; anything unreadable is ``(None, None)``."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    return _clean(data.get("by")), _clean(data.get("at"))


def _flag(values: dict[str, str], key: str) -> bool | None:
    value = values.get(key)
    return {"1": True, "0": False}.get(value) if value is not None else None


_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+!_-]{0,31}")


def _installed_version(raw: str | None) -> str | None:
    """*raw* when it is a version string, else None (it comes from a name the service user may own)."""
    from packaging.version import InvalidVersion, Version

    if not raw or not _VERSION_RE.fullmatch(raw):
        return None
    try:
        Version(raw)
    except InvalidVersion:
        return None
    return raw


def _predates_isolation(version: str) -> bool:
    """True when *version* is older than the release that has runner isolation."""
    from packaging.version import Version

    from dango.platform.cloud.compose_versions import MIN_RELEASE

    return Version(version).release < MIN_RELEASE


def parse_isolation_probe(stdout: str) -> IsolationStatus:
    """Parse :data:`ISOLATION_PROBE_CMD` output; unreadable output is ``unknown``.

    The markers decide the state (``disabled`` wins, as in ``run_as``).  The version record
    is secondary: a missing ``runner_isolation`` key is ``unknown``, never "off", because a
    later record write can drop it.
    """
    head, _, record_text = stdout.partition("\n---\n")
    values = dict(line.split("=", 1) for line in head.splitlines() if "=" in line)
    active, disabled = _flag(values, "active"), _flag(values, "disabled")
    record = "unknown"
    try:
        data = json.loads(record_text)
        raw = data.get("runner_isolation") if isinstance(data, dict) else None
        if isinstance(raw, dict) and raw.get("loopback") in ("restricted", "unrestricted"):
            record = str(raw["loopback"])
    except (ValueError, RecursionError):
        record = "unknown"
    nft = _flag(values, "nft")
    users = values.get("users")
    if active is None or disabled is None:
        return IsolationStatus(record=record)
    state = "disabled" if disabled else "on" if active else "missing"
    installed = _installed_version(values.get("installed"))
    if installed and state != "disabled" and _predates_isolation(installed):
        state = "unsupported"  # never claim what the installed version does not enforce
    by, at = _opt_out_info(values.get("disabled_info", "")) if disabled else (None, None)
    if nft is None:
        loopback = record
    else:
        loopback = "restricted" if nft else "unrestricted"
    return IsolationStatus(
        state=state,
        loopback=loopback,
        record=record,
        active_marker=active,
        disabled_marker=disabled,
        users=users if users in ("0", "1", "2") else None,
        sudoers=_flag(values, "sudoers"),
        nft=nft,
        disabled_by=by,
        disabled_at=at,
        installed=installed,
    )


def get_isolation(ssh: Any) -> IsolationStatus:
    """Probe the server as root; any failure is ``unknown``."""
    from dango.exceptions import CloudSSHError

    try:
        result = ssh.exec_command(ISOLATION_PROBE_CMD)
    except CloudSSHError:
        return IsolationStatus()
    if not result.success or not isinstance(result.stdout, str):
        return IsolationStatus()
    return parse_isolation_probe(result.stdout)


def format_isolation_line(status: IsolationStatus) -> str:
    """The text after ``Isolation:`` in ``remote status`` (Rich markup: marker text is escaped)."""
    if status.state == "on":
        return f"on (loopback {status.loopback})"
    if status.state == "disabled":
        who = f" by {escape(status.disabled_by)}" if status.disabled_by else ""
        when = f" at {escape(status.disabled_at)}" if status.disabled_at else ""
        return f"disabled (admin opt-out{who}{when})"
    if status.state == "missing":
        return "NOT enabled: run dango remote upgrade"
    if status.state == "unsupported":
        return (
            f"not enforced: the installed Dango {escape(status.installed or '?')} predates "
            "isolation (1.0.14): run dango remote upgrade"
        )
    return "unknown"


#: Reported by ``remote upgrade`` and setup when the opt-out marker is present.
DISABLED_LINE = (
    "Runner isolation: DISABLED by admin opt-out (run `dango remote isolation enable` to turn on)"
)
DISABLED_WARNING = (
    "Runner isolation is installed but DISABLED by an admin opt-out (/srv/dango/runner/disabled): "
    "scripts and notebooks still run as the dango service account. Run 'dango remote isolation "
    "enable' to turn it on, or 'dango remote isolation status' for details."
)


def disabled_probe_cmd(srv_dir: str = "/srv/dango") -> str:
    """Root command that prints ``disabled=1`` when a valid opt-out marker exists, else ``disabled=0``.

    Same test as :func:`dango.platform.run_as._marker_valid` and the main probe.
    """
    base = shlex.quote(srv_dir)
    runner = shlex.quote(f"{srv_dir}/runner")
    marker = shlex.quote(f"{srv_dir}/runner/disabled")
    return (
        f'if [ "$(find {base} {runner} -maxdepth 0 -type d -uid 0 ! -perm /022 2>/dev/null '
        f'| wc -l)" -eq 2 ] && [ -n "$(find {marker} -maxdepth 0 -type f -uid 0 '
        '! -perm /022 2>/dev/null)" ]; then echo disabled=1; else echo disabled=0; fi'
    )


DISABLED_PROBE_CMD = disabled_probe_cmd()


def disabled_marker_present(ssh: Any, srv_dir: str = "/srv/dango") -> bool:
    """True when the server has a valid opt-out marker; any failure is False."""
    try:
        result = ssh.exec_command(disabled_probe_cmd(srv_dir), timeout=30)
    except Exception:
        return False
    return bool(result.success) and "disabled=1" in str(result.stdout).split()
