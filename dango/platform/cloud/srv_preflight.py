"""dango/platform/cloud/srv_preflight.py

Refuse a BYOS host whose ``/srv`` or ``/srv/dango`` the service account could control.

Setup runs root scripts over ``/srv/dango``.  If that directory (or ``/srv``) already exists
and is owned by another user, is group/other-writable, or is a link or file, those scripts
would run over a tree the ``dango`` user can change.  Fresh hosts (path absent) and
root-owned, closed directories pass.  Runs before any other setup step.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from dango.exceptions import CloudProvisioningError

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

#: Exit status of the script when a path is unsafe (it prints ``ls -ld`` for that path).
EXIT_UNSAFE = 3
DEFAULT_PATHS = ("/srv", "/srv/dango")


def preflight_script(paths: tuple[str, ...] = DEFAULT_PATHS, owner: str = "root") -> str:
    """Shell that exits :data:`EXIT_UNSAFE` (printing ``ls -ld``) for the first unsafe path.

    A path is unsafe when it exists and is not a real directory owned by *owner*, or is
    writable by its group or by others.  ``find -maxdepth 0`` without ``-L`` never follows a
    link: a link is simply "not a directory".
    """
    return (
        f"for p in {' '.join(shlex.quote(p) for p in paths)}; do\n"
        '  if [ -e "$p" ] || [ -L "$p" ]; then\n'
        '    unsafe=$(find "$p" -maxdepth 0 \\( ! -type d -o ! -user '
        f"{shlex.quote(owner)} -o -perm -020 -o -perm -002 \\) -print) || exit 2\n"
        f'    if [ -n "$unsafe" ]; then ls -ld -- "$p"; exit {EXIT_UNSAFE}; fi\n'
        "  fi\n"
        "done\n"
    )


def check_srv_dango(ssh: SSHManager) -> None:
    """Raise :class:`CloudProvisioningError` unless ``/srv`` and ``/srv/dango`` are safe.

    Raises:
        CloudProvisioningError: A path is not a root-owned directory closed to other users,
            or the check itself could not run (fail closed).
    """
    result = ssh.exec_command(f"sh -c {shlex.quote(preflight_script())}", timeout=30)
    if result.success:
        return
    if result.exit_code != EXIT_UNSAFE:
        raise CloudProvisioningError(
            "Server setup stopped: /srv and /srv/dango could not be checked "
            f"(exit {result.exit_code}: {(result.stderr or result.stdout).strip()[:200]}). "
            "No change was made to the server."
        )
    listing = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "(unknown)"
    raise CloudProvisioningError(
        "Server setup stopped before changing anything: /srv/dango (or /srv) already exists "
        f"and is not a root-owned directory closed to other users ({listing}). Root setup "
        "would run over a tree the dango user can change. Inspect it as root, then either "
        "move it aside (mv /srv/dango /srv/dango.old) or fix it "
        "(chown root:root /srv/dango && chmod 755 /srv/dango), and run `dango deploy` again. "
        "An existing Dango server is brought up to date with `dango remote upgrade` instead."
    )
