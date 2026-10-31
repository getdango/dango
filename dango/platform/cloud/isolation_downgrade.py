"""dango/platform/cloud/isolation_downgrade.py

Runner isolation when ``remote upgrade`` installs a version older than the one that has it.

Dango before 1.0.14 runs scripts and notebooks as the service account and ignores the runner
users, the sudoers rule, the ACLs and the marker.  A marker left in place would make
``remote status`` and the 1.0.14 web code claim isolation that is not enforced, so the downgrade
takes the marker away (nothing else: users, files and rules stay) and says so.  The upgrade
to 1.0.14 or later writes the marker again through the normal steps, after verification.
"""

from __future__ import annotations

from typing import Any

from dango.platform.cloud import runner_access_scripts as access
from dango.platform.cloud import runner_isolation as ri
from dango.platform.cloud import runner_scripts as scripts

__all__ = ["demote", "not_enforced_line"]

#: First release that enforces runner isolation.
FIRST_RELEASE = "1.0.14"


def not_enforced_line(version: str) -> str:
    """The upgrade result line for a server now on *version*."""
    return (
        f"Runner isolation is not enforced by Dango {version} (it needs {FIRST_RELEASE} or later): "
        f"scripts and notebooks run as the dango service account. Upgrade to {FIRST_RELEASE} or "
        "later to turn it on again"
    )


def _marker_present(ssh: Any, srv_dir: str) -> bool:
    """True when the active marker exists; an unreadable answer counts as present (be safe)."""
    try:
        test = f"test -f {scripts.q(scripts.marker_path(srv_dir))}"
        return bool(ssh.exec_command(test).success)
    except Exception:
        return True


def demote(
    ssh: Any,
    warnings: list[str],
    version: str,
    guard: Any,
    *,
    own_guard: bool,
    srv_dir: str = scripts.SRV_DIR,
) -> str | None:
    """Remove the active marker of a server whose installed *version* does not enforce isolation.

    Returns the line for the upgrade result, or None when there was no marker.  A failure adds a
    loud warning and keeps the downgrade going (``remote status`` also checks the installed
    version, so it never reports isolation for it).  Idempotent: a repeated run finds no marker.
    """
    if not _marker_present(ssh, srv_dir):
        return None
    if own_guard:
        guard.arm()
    try:
        steps = [
            # root acts below srv_dir only after it is known that the service account cannot swap it
            ri.Step("ownership_check", access.ownership_precheck_script(srv_dir)),
            ri.Step("unenforce", scripts.disable_script(srv_dir)),
        ]
        outcome = ri.run_steps(steps, ri.executor(ssh, guard.rearm))
        if outcome.enabled:  # the record loses its isolation key now that no marker exists
            record = ri.Step("record", ri.record_script(srv_dir), on_failure="warn")
            ri.executor(ssh, None)(record)
    finally:
        if own_guard:
            guard.disarm()
    if outcome.enabled:
        return not_enforced_line(version)
    warnings.append(
        f"Dango {version} does not enforce runner isolation, but its marker could not be removed "
        f"({outcome.failed}: {outcome.detail or 'failed'}): scripts and notebooks run as the dango "
        f"service account. Run `dango remote upgrade --version {version} --force` again, or "
        f"upgrade to {FIRST_RELEASE} or later."
    )
    return None
