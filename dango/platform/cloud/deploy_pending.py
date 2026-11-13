"""dango/platform/cloud/deploy_pending.py

Server-side "build pending" marker for ``dango remote push``.

A push copies files first and builds afterwards.  Once the files are on the server,
a retry of the same push sees no difference, so it would omit the ``dbt deps`` and
``dbt run`` work that the failed push never finished.  The marker records that a push
started copying and has not finished building; the next push then forces the full
build.  It lives in ``/srv/dango`` next to the deploy lock, never in the project
directory the service user controls.
"""

from __future__ import annotations

import shlex
from typing import Any

from dango.exceptions import CloudProvisioningError

BUILD_PENDING_PATH = "/srv/dango/.deploy-build-pending"

_PRESENT = "build-pending"


def build_pending(ssh: Any) -> bool:
    """True if an earlier push copied files but did not finish building.

    Only the existence of the marker is read, never its content.
    """
    result = ssh.exec_command(
        f"if [ -e {shlex.quote(BUILD_PENDING_PATH)} ] || [ -L {shlex.quote(BUILD_PENDING_PATH)} ];"
        f" then echo {_PRESENT}; fi",
        timeout=30,
    )
    return _PRESENT in str(result.stdout or "")


def mark_build_pending(ssh: Any) -> None:
    """Record that a push is about to copy files (kept until the build succeeds).

    The marker is a directory made with ``mkdir``: ``mkdir`` never opens or follows what is
    at the name (it fails if anything, such as a planted link, is there), whereas a file
    create would open a link to a FIFO or device.  ``/srv/dango`` is root-owned on a
    server in the 1.0.14 state; a server still in the 1.0.13 state lets the service user
    rename entries in it.

    Raises:
        CloudProvisioningError: If the marker cannot be written; the push must not
            copy files it could not later tell were left unbuilt.
    """
    path = shlex.quote(BUILD_PENDING_PATH)
    result = ssh.exec_command(f"rm -rf -- {path} && mkdir -m 0700 -- {path}", timeout=30)
    if result.exit_code != 0:
        raise CloudProvisioningError(
            f"Could not record the pending build on the server: {(result.stderr or '').strip()}"
        )


def clear_build_pending(ssh: Any) -> bool:
    """Remove the marker after a successful build; False if it could not be removed."""
    result = ssh.exec_command(f"rm -rf -- {shlex.quote(BUILD_PENDING_PATH)}", timeout=30)
    return bool(result.exit_code == 0)
