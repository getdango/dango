"""dango/cli/commands/remote_isolation.py

``dango remote isolation status|disable|enable``: inspect, opt out of, and restore the restricted
users that run scripts and notebooks on a cloud server.

Everything happens over SSH as root, with the same guards as ``remote upgrade``: a server-side
dead-man timer restarts ``dango-web`` if this command dies, the ownership of ``/srv/dango`` is
checked before root writes below it, and root only creates, removes or reads files under the
root-owned ``/srv/dango/runner``.  Audit events are written on the server by the service account
(``runuser -u dango``, the venv interpreter in isolated mode), so root never imports or executes
anything the service account can write.  A failed audit write is a warning, never a failure.

The group is registered on ``remote`` by ``remote.py``.  This module does not import it, so
that import stays one-way.
"""

from __future__ import annotations

import getpass
import shlex
import uuid
from datetime import datetime, timezone
from typing import Any

import click
from rich.markup import escape

from dango.auth.audit import AuditEvent
from dango.cli import console
from dango.platform.cloud import runner_access_scripts as access
from dango.platform.cloud import runner_isolation as ri
from dango.platform.cloud import runner_scripts as scripts
from dango.platform.cloud.isolation_audit import (
    AUDIT_LOG_DIR,
    AUDIT_SOURCE,
    audit_command,
    journal_command,
    marker_prefix,
    ssh_actor,
)
from dango.platform.cloud.isolation_status import IsolationStatus, get_isolation
from dango.platform.cloud.upgrade_guard import GUARD_PREFIX, UpgradeGuard
from dango.platform.run_as import DISABLED_MARKER

__all__ = [
    "AUDIT_LOG_DIR",
    "AUDIT_SOURCE",
    "audit_command",
    "disable_marker_script",
    "isolation_group",
    "remove_disabled_script",
    "stop_web_script",
]

_STEP_TIMEOUT = 60
_CONFIRM_WORD = "disable"
_GROUP_STATES = ("group (fallback)", "group (legacy)", "migrating")


# ---------------------------------------------------------------------------
# Server-side scripts (root; they touch only <srv>/runner)
# ---------------------------------------------------------------------------


def _disabled_path(srv_dir: str = scripts.SRV_DIR) -> str:
    return (
        DISABLED_MARKER if srv_dir == scripts.SRV_DIR else f"{scripts.runner_dir(srv_dir)}/disabled"
    )


def stop_web_script() -> str:
    """Stop ``dango-web`` (and with it Marimo) and fail with 8 if it is still active."""
    return (
        "systemctl stop dango-web >/dev/null 2>&1 || true\n"
        "if systemctl is-active --quiet dango-web; then"
        ' echo "dango-web is still running" >&2; exit 8; fi\n'
    )


def disable_marker_script(
    srv_dir: str = scripts.SRV_DIR,
    *,
    by: str | None = None,
    at: str | None = None,
    server: str = "",
) -> str:
    """Write the opt-out marker: root:root 0644, regular file, temp file then ``mv -f``.

    Refuses a symlink at the runner directory, the marker or the temp file, and a directory at
    the marker path.  Only ``<srv>/runner`` is touched (never a path the service account owns).
    With *by* and *at* the marker's content records who and when (plus the SSH client address
    seen by the server) in one bounded line, and a copy goes to the journal (best effort).  The
    content is informational: nothing trusts it for a decision.
    """
    base = scripts.runner_dir(srv_dir)
    marker = _disabled_path(srv_dir)
    tmp = f"{base}/.disabled.tmp"
    q = shlex.quote
    if by is None:
        content, journal = f"printf '{{\"schema\": 1}}\\n' >{q(tmp)}\n", ""
    else:
        prefix = marker_prefix(by, at or "", server)
        content = (
            "S_C=${SSH_CLIENT:-}\n"
            "C=$(printf %s \"${S_C%% *}\" | tr -cd '0-9a-fA-F.:' | head -c 64)\n"
            f'printf \'%s, "client": "%s"}}\\n\' {q(prefix)} "$C" >{q(tmp)}\n'
        )
        journal = journal_command(AuditEvent.ISOLATION_DISABLED, by, at or "", server)
    return (
        "set -eu\n"
        + scripts._guard([base, marker, tmp])
        + f"[ ! -d {q(marker)} ] || {{ echo 'marker path is a directory' >&2; exit 3; }}\n"
        + f"install -d -o root -g root -m 0755 {q(base)}\n"
        + f"rm -f {q(tmp)}\numask 022\n"
        + content
        + f"chown root:root {q(tmp)}\nchmod 0644 {q(tmp)}\nmv -f {q(tmp)} {q(marker)}\n"
        + journal
    )


def remove_disabled_script(srv_dir: str = scripts.SRV_DIR) -> str:
    """Remove the opt-out marker (only ever run after the isolation steps and their verify passed)."""
    base = scripts.runner_dir(srv_dir)
    marker = shlex.quote(_disabled_path(srv_dir))
    return (
        "set -eu\n"
        + scripts._guard([base])
        + f"rm -f {marker}\n"
        + f"[ ! -e {marker} ] && [ ! -L {marker} ] || {{ echo 'marker still present' >&2; exit 3; }}\n"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connect(ctx: click.Context) -> tuple[Any, Any, str]:
    """Load the cloud config, connect as root; returns ``(config, ssh, actor)``, exits 1 on failure."""
    from dango.cli.commands.remote_mgmt import _load_cloud_config_with_ip, _make_ssh_manager

    cloud_cfg, project_root = _load_cloud_config_with_ip(ctx)
    ssh = _make_ssh_manager(cloud_cfg, project_root)
    try:
        ssh.connect(cloud_cfg.droplet_ip)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Cannot connect to server: {escape(str(exc))}")
        raise SystemExit(1) from exc
    actor = ssh_actor(cloud_cfg, project_root, str(cloud_cfg.droplet_ip), getpass.getuser())
    return cloud_cfg, ssh, actor


class _OwnedGuard(UpgradeGuard):
    """An :class:`UpgradeGuard` whose unit names say which isolation command armed them."""

    def __init__(self, ssh: Any, warnings: list[str], owner: str) -> None:
        super().__init__(ssh, warnings)
        self.owner = owner
        # a random suffix tells two runs of the same command in the same second apart
        self.tag = f"{owner}-{self._run_id}-{uuid.uuid4().hex[:6]}"
        self._run_id = self.tag


_LIST_GUARDS = f"systemctl list-units --no-legend --plain '{GUARD_PREFIX}-*' 2>/dev/null; true"
#: A same-command guard armed less than this long ago belongs to a live run: its steps time out
#: after 60 s and the guard is re-armed after each one.  Older means the run was killed.
_LIVE_WITHIN = 300
_AGE_CMD = (
    "t=$(systemctl show -p ActiveEnterTimestamp --value '{unit}' 2>/dev/null) && [ -n \"$t\" ]"
    ' && echo $(( $(date +%s) - $(date -d "$t" +%s) ))'
)
_OWNERS = {
    "isolation-disable": "dango remote isolation disable",
    "isolation-enable": "dango remote isolation enable",
}


def _guard_units(ssh: Any) -> list[str]:
    """Names of the guard units systemd lists (empty when none or unreadable)."""
    try:
        out = str(ssh.exec_command(_LIST_GUARDS, timeout=30).stdout or "")
    except Exception:
        return []
    return [line.split()[0] for line in out.splitlines() if line.split()]


def _owner_of(unit: str) -> str:
    """The command that armed *unit*: an isolation owner key, or ``upgrade``."""
    return next((k for k in _OWNERS if f"{GUARD_PREFIX}-{k}-" in unit), "upgrade")


def _age(ssh: Any, unit: str) -> int | None:
    """Seconds since the guard *unit* was last armed, or None when the server cannot say."""
    try:
        out = str(ssh.exec_command(_AGE_CMD.format(unit=unit), timeout=30).stdout or "").strip()
        return int(out)
    except Exception:
        return None


def _refuse_if_busy(ssh: Any, owner: str, tag: str | None = None) -> bool:
    """Exit 1 when another operation holds an upgrade guard; True when a stale leftover was seen.

    A guard of *owner* itself that is clearly stale (armed more than ``_LIVE_WITHIN`` seconds ago)
    is the leftover of a killed run of the same command: arming replaces it.  A recent one, or one
    whose age is unknown, may be a live run of the same command, so it blocks like any other
    operation's guard.  This is a check, not a lock: an operation that starts between the check
    and the arm is only caught by the second call right after arming (*tag* is this run's guard).
    """
    units = [u for u in _guard_units(ssh) if tag is None or tag not in u]
    busy = []
    for unit in units:
        age = _age(ssh, unit) if _owner_of(unit) == owner else None
        if age is None or age <= _LIVE_WITHIN:
            busy.append(unit)
    if busy:
        who = _owner_of(busy[0])
        label = f"`{_OWNERS[who]}`" if who in _OWNERS else "`dango remote upgrade`"
        wait = "5 minutes" if who == owner else "20 minutes"
        console.print(
            f"[red]Error:[/red] {label} seems to be running on this server, or was interrupted "
            f"(guard {escape(busy[0])}; it expires within {wait}). Wait for it to finish "
            "and run the command again."
        )
        raise SystemExit(1)
    return bool(units)


def _arm_guard(ssh: Any, warnings: list[str], owner: str) -> _OwnedGuard:
    """Check, arm and check again; exits 1 when another operation holds a guard."""
    leftover = _refuse_if_busy(ssh, owner)
    guard = _OwnedGuard(ssh, warnings, owner)
    guard.arm()
    if leftover:
        console.print(
            f"Removed a leftover guard from an earlier `{_OWNERS[owner]}` that did not finish."
        )
    # an operation that armed in the gap has cancelled ours (or will): its guard is not ours to stop
    _refuse_if_busy(ssh, owner, guard.tag)
    return guard


def _require_known(status: IsolationStatus) -> None:
    if status.state == "unknown":
        console.print(
            "[red]Error:[/red] Could not read the isolation state from the server. "
            "Check connectivity with [bold]dango remote status[/bold]."
        )
        raise SystemExit(1)


def _executor(ssh: Any, guard: Any) -> Any:
    return ri.executor(ssh, guard.rearm)


def _record_audit(ssh: Any, event: AuditEvent, ip: str, actor: str) -> None:
    """Write the audit event on the server; a failure only prints a warning.

    A fresh nonce is read back, so a stale identical line cannot count as success.
    """
    command = audit_command(event, actor, ip, uuid.uuid4().hex)
    try:
        ok = bool(ssh.exec_command(command, timeout=_STEP_TIMEOUT).success)
    except Exception:
        ok = False
    if not ok:
        console.print(
            f"[yellow]Warning:[/yellow] The {event.value} audit event could not be written "
            "on the server; the change itself was applied."
        )


def _print_warnings(warnings: list[str]) -> None:
    for warning in dict.fromkeys(warnings):
        console.print(f"[yellow]Warning:[/yellow] {escape(warning)}")


def _exposure_warning(docker_access: str) -> list[str]:
    lines = [
        "With isolation disabled, scripts and notebooks run as the dango service account: they "
        "can read this server's secrets (.env, .dlt), backups and warehouse, and reach its "
        "local services.",
    ]
    if docker_access in _GROUP_STATES:
        lines.append(
            f"Docker access state: {docker_access}. The dango account is in the docker group, so "
            "scripts and notebooks (and therefore Editors) are root-equivalent."
        )
    elif docker_access == "helper":
        lines.append(
            "Docker access state: helper. The dango account is not in the docker group, so this "
            "is not root-equivalent."
        )
    else:
        lines.append(
            f"Docker access state: {docker_access}. Whether scripts and notebooks are "
            "root-equivalent could not be determined."
        )
    return lines


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@click.group("isolation")
def isolation_group() -> None:
    """Inspect, disable and re-enable script and notebook isolation on the server."""


@isolation_group.command("status")
@click.pass_context
def isolation_status(ctx: click.Context) -> None:
    """Show whether scripts and notebooks run as restricted users on the server.

    Example:
      dango remote isolation status
    """
    _, ssh, _actor = _connect(ctx)
    try:
        status = get_isolation(ssh)
    finally:
        ssh.disconnect()
    yes_no = {True: "yes", False: "no", None: "unknown"}
    console.print(f"Isolation: {status.state}")
    if status.disabled_by or status.disabled_at:
        console.print(
            f"Opt-out recorded by: {escape(status.disabled_by or '?')} at "
            f"{escape(status.disabled_at or '?')}"
        )
    console.print(f"Loopback: {status.loopback}")
    console.print("Runner pieces:")
    console.print(f"  Runner users: {status.users or 'unknown'} of 2 present")
    console.print(f"  Sudoers rule: {yes_no[status.sudoers]}")
    console.print(f"  Firewall table (inet dango_runner) loaded: {yes_no[status.nft]}")
    console.print(f"  Active marker valid: {yes_no[status.active_marker]}")
    console.print(f"  Disabled marker valid: {yes_no[status.disabled_marker]}")
    console.print(f"  Version record loopback: {status.record}")
    if status.state == "missing":
        console.print("Run [bold]dango remote upgrade[/bold] to enable isolation.")
    elif status.state == "disabled":
        console.print("Run [bold]dango remote isolation enable[/bold] to turn it back on.")


@isolation_group.command("disable")
@click.option("--yes", "-y", is_flag=True, help="Skip the typed confirmation.")
@click.pass_context
def isolation_disable(ctx: click.Context, yes: bool) -> None:
    """Opt this server out of script and notebook isolation (audited).

    Scripts and notebooks then run with the dango service account's access again.

    Example:
      dango remote isolation disable --yes
    """
    from dango.platform.cloud.server_status import docker_access_state

    cloud_cfg, ssh, actor = _connect(ctx)
    ip = str(cloud_cfg.droplet_ip)
    try:
        status = get_isolation(ssh)
        _require_known(status)
        if status.state == "disabled":
            # the marker is in force; a re-run still ends leftover runner processes
            swept = ri.run_steps(
                [ri.Step("quiesce", access.quiesce_script(stop_web=False))], ri.executor(ssh, None)
            )
            if swept.failed:
                console.print(
                    "[red]Error:[/red] Isolation is already disabled, but runner processes could "
                    f"not be ended ({swept.detail or swept.failed}). Run the command again."
                )
                raise SystemExit(1)
            console.print(
                "Isolation is already disabled on this server; leftover runner processes, if "
                "any, were ended."
            )
            return
        for line in _exposure_warning(docker_access_state(ssh)):
            console.print(f"[yellow]Warning:[/yellow] {line}")
        if not yes:
            typed = click.prompt(
                f"Type '{_CONFIRM_WORD}' to continue", default="", show_default=False
            )
            if typed.strip().lower() != _CONFIRM_WORD:
                console.print("Aborted. Isolation was not changed.")
                raise click.Abort()
        warnings: list[str] = []
        guard = _arm_guard(ssh, warnings, "isolation-disable")
        was_active = ri.web_is_active(ssh)
        steps = [
            ri.Step("ownership_check", access.ownership_precheck_script()),
            ri.Step("stop_web", stop_web_script()),
            ri.Step(
                "disable_marker",
                disable_marker_script(
                    by=actor, at=datetime.now(timezone.utc).isoformat(timespec="seconds"), server=ip
                ),
            ),
            # runner processes that outlived the stop are ended before the service returns
            ri.Step("quiesce", access.quiesce_script(stop_web=False)),
        ]
        try:
            outcome = ri.run_steps(steps, _executor(ssh, guard))
        finally:
            if was_active:
                ri.start_web(ssh, warnings)
            guard.disarm()
        _print_warnings(warnings)
        in_force = "disable_marker" in outcome.attempted and outcome.failed != "disable_marker"
        if in_force:
            _record_audit(ssh, AuditEvent.ISOLATION_DISABLED, ip, actor)
        if outcome.failed is None:
            console.print(
                "[green]Isolation disabled.[/green] Scripts and notebooks now run as dango."
            )
            return
        if in_force:
            console.print(
                "[red]Error:[/red] Isolation is disabled, but runner processes could not be ended "
                f"({escape(outcome.detail or outcome.failed or '')}). Run the command again."
            )
        else:
            console.print(
                f"[red]Error:[/red] Isolation was not changed: {outcome.failed} failed "
                f"({escape(outcome.detail or 'no detail')})."
            )
        raise SystemExit(1)
    finally:
        ssh.disconnect()


@isolation_group.command("enable")
@click.pass_context
def isolation_enable(ctx: click.Context) -> None:
    """Turn script and notebook isolation back on (removes the opt-out; audited).

    Runs the same steps as ``remote upgrade``, including the verification.  The opt-out marker
    is removed only after they pass; otherwise the server stays as it was.

    Example:
      dango remote isolation enable
    """
    cloud_cfg, ssh, actor = _connect(ctx)
    ip = str(cloud_cfg.droplet_ip)
    try:
        status = get_isolation(ssh)
        _require_known(status)
        if status.state == "on":
            console.print("Isolation is already enabled on this server; nothing to do.")
            return
        if not ri.build_present(ssh):
            console.print(
                f"[red]Error:[/red] {ri.ISOLATION_MARK}: {ri.BUILD_MISSING}. "
                "Run [bold]dango remote upgrade[/bold] first."
            )
            raise SystemExit(1)
        warnings: list[str] = []
        guard = _arm_guard(ssh, warnings, "isolation-enable")
        was_active = ri.web_is_active(ssh)
        removed = True
        try:
            outcome = ri.run_steps(ri.build_steps(), _executor(ssh, guard))
            execute = ri.executor(ssh, None)
            if outcome.enabled and status.state == "disabled":
                removed = execute(ri.Step("remove_disabled", remove_disabled_script())).ok
            if outcome.failed != "ownership_check":
                record = ri.Step("record", ri.record_script(), on_failure="warn")
                execute(record)
        finally:
            if was_active:
                ri.start_web(ssh, warnings)
            guard.disarm()
        warnings.extend(outcome.warnings)
        _print_warnings(warnings)
        if outcome.enabled and removed:
            _record_audit(ssh, AuditEvent.ISOLATION_ENABLED, ip, actor)
            console.print(f"[green]{escape(ri.status_line(outcome))}[/green]")
            return
        if outcome.enabled:
            console.print(
                "[red]Error:[/red] The isolation steps passed, but the opt-out marker could not "
                "be removed; the server is still disabled. Run the command again."
            )
        else:
            kept = (
                "The server stays disabled." if status.state == "disabled" else "Nothing changed."
            )
            console.print(f"[red]Error:[/red] {escape(ri.status_line(outcome))}. {kept}")
        raise SystemExit(1)
    finally:
        ssh.disconnect()
