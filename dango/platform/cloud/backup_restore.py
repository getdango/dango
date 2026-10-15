"""dango/platform/cloud/backup_restore.py

Copy helpers shared by the cloud backup and restore paths.

Shell builders copy only sources that exist and never swallow a failed ``cp``.
Restores run as root, but the project tree belongs to the service user, so project
files are written as that user; root writes only the Metabase volume, refusing
symlinked destinations there.  ``wal_sidecar_names`` lists the WAL files a restore
must clear for a database.
"""

from __future__ import annotations

import shlex


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
