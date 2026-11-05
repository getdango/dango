"""dango/platform/cloud/srv_preflight.py

Refuse a BYOS host whose ``/srv`` or ``/srv/dango`` the service account could control.

Setup runs root scripts over ``/srv/dango``.  If that directory (or ``/srv``) already exists
and is owned by another user, is group/other-writable, carries an ACL, or is a link or file,
those scripts would run over a tree the ``dango`` user can change.  Fresh hosts (path absent)
and root-owned, closed directories without an ACL pass.  Runs before any other setup step.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from dango.exceptions import CloudProvisioningError

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

#: Exit status of the script when a path is unsafe (it prints the path, then ``ls -ld``).
EXIT_UNSAFE = 3
DEFAULT_PATHS = ("/srv", "/srv/dango")


def preflight_script(paths: tuple[str, ...] = DEFAULT_PATHS, owner: str = "root") -> str:
    """Shell that exits :data:`EXIT_UNSAFE` for the first unsafe path, printing it, then ``ls -ld``.

    A path is unsafe when it exists and is not a real directory owned by *owner*, is writable
    by its group or by others, or has an extended or default ACL (``ls -l`` shows a ``+``
    after the mode; no ``getfacl`` is needed, so its absence cannot make the check pass).
    ``find -maxdepth 0`` without ``-L`` never follows a link: a link is "not a directory".
    A check that cannot read the path or its listing exits 2 (the caller refuses).
    """
    return (
        f"for p in {' '.join(shlex.quote(p) for p in paths)}; do\n"
        '  if [ -e "$p" ] || [ -L "$p" ]; then\n'
        '    unsafe=$(find "$p" -maxdepth 0 \\( ! -type d -o ! -user '
        f"{shlex.quote(owner)} -o -perm -020 -o -perm -002 \\) -print) || exit 2\n"
        '    listing=$(ls -ld -- "$p") || exit 2\n'
        '    case "$listing" in ??????????+*) acl=1;; *) acl=;; esac\n'
        '    if [ -n "$unsafe" ] || [ -n "$acl" ]; then\n'
        '      printf "%s\\n" "$p"; printf "%s\\n" "$listing"; exit '
        f"{EXIT_UNSAFE}\n"
        "    fi\n"
        "  fi\n"
        "done\n"
    )


def _refusal(path: str, listing: str) -> str:
    """The refusal text for the unsafe *path*; the advice fits that path only."""
    what = (
        f"Server setup stopped before changing anything: {path} already exists and is not a "
        "root-owned directory closed to other users and without an ACL "
        f"({listing}). Root setup would run over a tree the dango user can change. "
    )
    if path == "/srv/dango":
        return what + (
            "To update an existing Dango server use `dango remote upgrade`. For a new server "
            "use a fresh host, or have root move it aside (mv /srv/dango /srv/dango.old) after "
            "checking it, and run `dango deploy` again."
        )
    return what + (
        "Use a fresh host, or have root restore /srv to a root-owned directory (mode 755, no "
        "ACL) after checking its contents, and run `dango deploy` again."
    )


def check_srv_dango(ssh: SSHManager) -> None:
    """Raise :class:`CloudProvisioningError` unless ``/srv`` and ``/srv/dango`` are safe.

    Raises:
        CloudProvisioningError: A path is not a root-owned directory closed to other users
            and free of ACLs, or the check itself could not run (fail closed).
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
    lines = result.stdout.strip().splitlines()
    path = lines[0].strip() if lines and lines[0].strip() in DEFAULT_PATHS else "/srv/dango"
    listing = lines[-1].strip() if len(lines) > 1 else "(unknown)"
    raise CloudProvisioningError(_refusal(path, listing))
