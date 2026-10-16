"""dango/platform/cloud/backup_restore.py

Copy helpers shared by the cloud backup and restore paths.

Shell builders copy only sources that exist and never swallow a failed ``cp``.
Restores run as root, but the project tree belongs to the service user, so project
files are written as that user; root writes only the Metabase volume, refusing
symlinked destinations there.  ``wal_sidecar_names`` lists the WAL files a restore
must clear for a database.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager


def wal_sidecar_names(db_rel_path: str) -> list[str]:
    """Return the SQLite/DuckDB WAL sidecar paths for a database file (relative path in, relative paths out)."""
    if db_rel_path.endswith(".duckdb"):
        return [db_rel_path + ".wal"]
    if db_rel_path.endswith(".db"):
        return [db_rel_path + "-wal", db_rel_path + "-shm"]
    return []


def copy_file_cmd(src: str, dest_dir: str) -> str:
    """Shell step copying *src* into *dest_dir* if it exists; a failed copy fails the step."""
    q_src, q_dir = shlex.quote(src), shlex.quote(dest_dir)
    return f"if [ -f {q_src} ]; then mkdir -p {q_dir} && cp {q_src} {q_dir}/; fi"


def copy_dir_cmd(src: str, dest: str) -> str:
    """Shell step copying the contents of *src* into *dest* if it exists; a failed copy fails the step."""
    q_dest = shlex.quote(dest)
    return (
        f"if [ -d {shlex.quote(src)} ]; then mkdir -p {q_dest}"
        f" && cp -r {shlex.quote(src + '/.')} {q_dest}/; fi"
    )


#: Owner of the project tree on the server; restores write project files as this user.
SERVICE_USER = "dango"
#: Shell prefix running a command as :data:`SERVICE_USER` (restores run as root).
AS_SERVICE_USER = f"runuser -u {SERVICE_USER} --"
#: Staging name of the sanitized Metabase metadata (written by root, outside the project).
SANITIZED_METABASE_NAME = "metabase.sanitized.yml"


def hand_staging_to_service_user_cmd(staging: str) -> str:
    """Shell step giving the extracted archive (except root-only ``metabase/``) to the service user.

    The staging directory itself stays root-owned, so its entries cannot be swapped.
    """
    return (
        f"find {shlex.quote(staging)} -mindepth 1 -maxdepth 1 ! -name metabase"
        f" -exec chown -R {SERVICE_USER}:{SERVICE_USER} {{}} +"
    )


def restore_file_cmd(src: str, dest: str, before: list[str] | None = None) -> str:
    """Shell step restoring file *src* to project path *dest* as the service user.

    Writes are bounded by the service user's own permissions, so a symlink it planted
    cannot redirect them.  *before* holds shell commands run first, only when *src* exists.
    """
    q_dest = shlex.quote(dest)
    steps = [
        *(before or []),
        f"{AS_SERVICE_USER} mkdir -p {shlex.quote(dest.rsplit('/', 1)[0])}",
        f"{AS_SERVICE_USER} cp {shlex.quote(src)} {q_dest}",
    ]
    return f"if [ -f {shlex.quote(src)} ]; then {' && '.join(steps)}; fi"


def restore_dir_cmd(src: str, dest: str) -> str:
    """Shell step merging directory *src* into project path *dest* as the service user."""
    q_dest = shlex.quote(dest)
    return (
        f"if [ -d {shlex.quote(src)} ]; then {AS_SERVICE_USER} mkdir -p {q_dest}"
        f" && {AS_SERVICE_USER} cp -r {shlex.quote(src + '/.')} {q_dest}/; fi"
    )


def restore_volume_file_cmd(src: str, volume: str, name: str) -> str:
    """Shell step copying *src* into a root-written volume; refuses symlinked destinations."""
    dest = f"{volume}/{name}"
    q_vol, q_dest = shlex.quote(volume), shlex.quote(dest)
    refusal = shlex.quote(f"Refusing to write through a symlink: {dest}")
    return (
        f"if [ -f {shlex.quote(src)} ]; then"
        f" if [ -L {q_vol} ] || [ -L {q_dest} ]; then echo {refusal} >&2; false;"
        f" else cp {shlex.quote(src)} {q_dest}; fi; fi"
    )


#: GNU/BSD tar options for root extraction: archive owners and modes are not applied,
#: so nothing extracted is writable by another user.
SAFE_EXTRACT_OPTIONS = "--strip-components=1 --no-same-owner --no-same-permissions"
UNSAFE_ARCHIVE_MESSAGE = (
    "Archive contains links or special files, which a restore never writes: {names}. "
    "Nothing was restored."
)


def find_unsafe_cmd(staging: str) -> str:
    """Shell step that fails, listing them, if *staging* holds anything but plain files and dirs.

    Symlinks, devices, FIFOs and hard-linked files are all reported.
    """
    return (
        f"! find {shlex.quote(staging)} \\( ! -type f ! -type d \\) -print -o"
        " -type f -links +1 -print | head -n 20 | grep ."
    )


def unsafe_tree_entries(root: Path) -> list[str]:
    """Return entries below *root* that are not plain files or directories (never followed)."""
    unsafe: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                unsafe.append(str(path.relative_to(root)))
    return unsafe


def unsafe_tar_members(archive: Path) -> list[str]:
    """Return members of *archive* that are not plain files or directories."""
    import tarfile

    with tarfile.open(archive, mode="r:*") as tf:
        return [m.name for m in tf.getmembers() if not (m.isfile() or m.isdir())]


def extract_checked_archive(
    ssh: SSHManager, archive_path: str, staging: str, content: list[str]
) -> None:
    """Extract *archive_path* into *staging* over SSH (as root) and check what came out.

    Root later reads staged files, so links and special files are refused outright.
    The archive must also hold ``manifest.json`` and at least one of *content*
    (paths relative to the archive's top-level directory).  On any failure the
    staging directory is removed and ``CloudProvisioningError`` is raised.
    """
    from dango.exceptions import CloudProvisioningError

    q_staging = shlex.quote(staging)
    try:
        result = ssh.exec_command(
            f"mkdir -p {q_staging} && tar -xzf {shlex.quote(archive_path)} -C {q_staging}"
            f" {SAFE_EXTRACT_OPTIONS}",
            timeout=300,
        )
        if not result.success:
            raise CloudProvisioningError(
                f"Backup step 'extract_archive' failed (exit {result.exit_code}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        found = ssh.exec_command(find_unsafe_cmd(staging), timeout=120)
        if not found.success:
            unsafe = found.stdout.split() or ["(could not be checked)"]
            names = ", ".join(path.replace(f"{staging}/", "", 1) for path in unsafe)
            raise CloudProvisioningError(UNSAFE_ARCHIVE_MESSAGE.format(names=names))

        # Hand-made archives without a single top-level directory extract to
        # nothing recognisable; fail before any copy rather than report success.
        layout_check = (
            f"test -e {shlex.quote(staging + '/manifest.json')} && ( "
            + " || ".join(f"test -e {shlex.quote(f'{staging}/{rel}')}" for rel in content)
            + " )"
        )
        if not ssh.exec_command(layout_check).success:
            raise CloudProvisioningError(
                "Archive does not look like a Dango backup (unexpected layout): "
                "nothing was restored"
            )
    except Exception:
        with contextlib.suppress(Exception):  # cleanup: best effort, keep the original error
            ssh.exec_command(f"rm -rf {q_staging}")
        raise
