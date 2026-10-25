"""dango/platform/cloud/upgrade_guard.py

Server-side dead-man guard for ``remote upgrade``.

The upgrade stops ``dango-web`` and restarts it from the client.  If the client
dies in between, nothing would start it again.  The guard is a transient systemd
timer on the server that starts ``dango-web`` unless the client keeps pushing it
back: it is re-armed after every completed step, so the window means "minutes
since the last completed step".  When the timer fires while an upgrade step
(pip, migrations, runner-user setup, Docker rebuild) is still running, the action waits for it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from dango.logging import get_logger

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

_logger = get_logger(__name__)

GUARD_PREFIX = "dango-upgrade-guard"
GUARD_WINDOW = "20m"

# Stops every guard timer and any fired, still-waiting service from this or an earlier run.
DISARM_ALL = (
    f"systemctl stop '{GUARD_PREFIX}-*.timer' '{GUARD_PREFIX}-*.service' 2>/dev/null; "
    f"systemctl reset-failed '{GUARD_PREFIX}-*' 2>/dev/null; true"
)

# Exit 0 when an upgrade guard (timer or fired, still-waiting service) is active: a ``remote
# upgrade`` is probably running, and arming another guard would cancel its dead-man timer.
GUARD_ACTIVE_CMD = (
    f"systemctl list-units --no-legend --plain '{GUARD_PREFIX}-*' 2>/dev/null | grep -q ."
)

# The guard action waits (120 x 15 s = 30 min at most) while an upgrade step still runs, then
# starts dango-web.  The [p]/[d]/[a] brackets keep pgrep from matching this sh's own command line.
GUARD_ACTION = (
    "/bin/sh -c 'n=0; while [ $n -lt 120 ] && { "
    'pgrep -f "/srv/dango/venv/bin/[p]ip install" >/dev/null || '
    'pgrep -f "[a]pply_all_pending" >/dev/null || '
    'pgrep -f "/srv/dango/venv/bin/[p]ython.* -m (pip|spacy)" >/dev/null || '
    'pgrep -f "[p]ython3 -m venv /srv/dango" >/dev/null || '
    'pgrep -f "[a]pt-get (install|update)" >/dev/null || '
    'pgrep -f "[u]seradd|[g]roupadd" >/dev/null || '
    'pgrep -f "[v]isudo" >/dev/null || '
    'pgrep -f "[s]etfacl" >/dev/null || '
    'pgrep -f "[/]usr/sbin/[n]ft -" >/dev/null || '
    'pgrep -f "[d]ocker compose.*build" >/dev/null || '
    'pgrep -f "[d]ango-compose (up-build|build|pull)" >/dev/null; }; do n=$((n+1)); sleep 15; done; '
    "systemctl start dango-web'"
)


def guard_unit(run_id: str, step: int = 1) -> str:
    """Unit name for the *step*-th arming of the upgrade run *run_id*."""
    return f"{GUARD_PREFIX}-{run_id}" if step == 1 else f"{GUARD_PREFIX}-{run_id}-{step}"


def arm_command(unit: str) -> str:
    """Shell command that arms *unit* (collected by systemd once it has run or failed)."""
    return f"systemd-run --collect --unit={unit} --on-active={GUARD_WINDOW} {GUARD_ACTION}"


class UpgradeGuard:
    """Arm, re-arm and disarm the server-side guard; failures are warnings, never errors."""

    def __init__(self, ssh: SSHManager, warnings: list[str]) -> None:
        self._ssh = ssh
        self._warnings = warnings
        self._run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._step = 0
        self._unit: str | None = None

    def arm(self) -> None:
        """Disarm guards from any earlier run, then arm a fresh one."""
        self._run(DISARM_ALL)
        self._arm_next()

    def rearm(self) -> None:
        """Restart the window after a completed step (new unit; the previous one is stopped).

        The new unit is armed first, so a failure or a dying client never leaves a gap.
        """
        previous = self._unit
        if self._arm_next() and previous is not None:
            self._run(f"systemctl stop '{previous}.timer' '{previous}.service' 2>/dev/null; true")

    def disarm(self) -> None:
        """Stop every guard unit (call once the client has restarted the services itself)."""
        self._unit = None
        self._run(DISARM_ALL)

    def _arm_next(self) -> bool:
        self._step += 1
        unit = guard_unit(self._run_id, self._step)
        armed = self._run(arm_command(unit))
        if armed:
            self._unit = unit
        return armed

    def _run(self, command: str) -> bool:
        try:
            ok = self._ssh.exec_command(command, timeout=30).success
        except Exception:
            _logger.warning("upgrade_guard_command_failed", exc_info=True)
            ok = False
        if not ok and command.startswith("systemd-run"):
            msg = (
                "Could not arm the server-side upgrade guard; if this command is interrupted, "
                "run `dango remote upgrade` again to bring the services back."
            )
            if msg not in self._warnings:
                self._warnings.append(msg)
        return ok
