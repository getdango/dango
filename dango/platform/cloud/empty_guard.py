"""dango/platform/cloud/empty_guard.py

Refuse to mirror an empty local directory over a non-empty server copy.

``rsync --delete`` of an empty directory erases everything in the server copy.
The sync skips a missing local directory and an empty one on both sides is
harmless, so only "local empty, server not" is refused.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.project_files import AS_DANGO

_JUNK = frozenset({".DS_Store", ".gitkeep", "Thumbs.db"})

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager


def refuse_empty_mirrors(
    ssh: SSHManager,
    local_project_root: Path,
    rel_dirs: Iterable[str],
    remote_project_dir: str,
    *,
    allow_empty: bool,
) -> None:
    """Raise if a local sync directory is empty while its server copy is not.

    Args:
        ssh: Connected SSHManager.
        local_project_root: Local project root.
        rel_dirs: Project-relative directories that are mirrored with ``--delete``.
        remote_project_dir: Project directory on the server.
        allow_empty: Skip the check (the caller accepts erasing the server copy).

    Raises:
        CloudProvisioningError: If a local directory exists, holds no real files, and the
            server copy has files.
    """
    if allow_empty:
        return
    for rel in rel_dirs:
        local_dir = local_project_root / rel
        if not local_dir.is_dir() or any(
            p.is_file() and p.name not in _JUNK for p in local_dir.rglob("*")
        ):
            continue
        target = shlex.quote(f"{remote_project_dir}/{rel}")
        probe = f'test -n "$(find {target} -type f -print -quit 2>/dev/null)"'
        if ssh.exec_command(f"{AS_DANGO} sh -c {shlex.quote(probe)}").exit_code == 0:
            raise CloudProvisioningError(
                f"Local {rel}/ is empty but the server copy is not; rsync --delete would "
                "erase it. Restore the local files or pass --allow-empty."
            )
