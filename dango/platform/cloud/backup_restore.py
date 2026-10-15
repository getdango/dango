"""dango/platform/cloud/backup_restore.py

Copy helpers shared by the cloud backup and restore paths.

Shell builders (for commands run over SSH by ``backup.py``) copy only sources
that exist and never swallow a failed ``cp``.  Restore destinations live in
trees the ``dango`` user can write, while restores run as root, so restore
helpers replace symlinks in the destination instead of following them.
``wal_sidecar_names`` lists the WAL files a restore must clear for a database.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path


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


def unlink_symlinks_cmd(root: str, rel: str) -> str:
    """Shell step removing symlinks at ``root/rel`` and at each of its parents below *root*."""
    parts = [part for part in rel.split("/") if part]
    steps = []
    for depth in range(1, len(parts) + 1):
        path = shlex.quote(f"{root}/{'/'.join(parts[:depth])}")
        steps.append(f"if [ -L {path} ]; then rm -f {path}; fi")
    return " && ".join(steps)


def restore_file_cmd(src: str, root: str, rel: str, before: list[str] | None = None) -> str:
    """Shell step restoring file *src* to ``root/rel`` without following destination symlinks.

    *before* holds shell commands run first, only when *src* exists.  The destination
    is removed before copying (portable ``cp --remove-destination``).
    """
    dest = f"{root}/{rel}"
    q_dest = shlex.quote(dest)
    steps = [unlink_symlinks_cmd(root, rel), *(before or [])]
    steps += [
        f"mkdir -p {shlex.quote(dest.rsplit('/', 1)[0])}",
        f"rm -f {q_dest}",
        f"cp {shlex.quote(src)} {q_dest}",
    ]
    return f"if [ -f {shlex.quote(src)} ]; then {' && '.join(steps)}; fi"


def restore_dir_cmd(src: str, root: str, rel: str) -> str:
    """Shell step merging directory *src* into ``root/rel`` after removing symlinks there."""
    q_dest = shlex.quote(f"{root}/{rel}")
    steps = [
        unlink_symlinks_cmd(root, rel),
        f"mkdir -p {q_dest}",
        f"find {q_dest} -xdev -type l -delete",
        f"cp -r {shlex.quote(src + '/.')} {q_dest}/",
    ]
    return f"if [ -d {shlex.quote(src)} ]; then {' && '.join(steps)}; fi"


def clear_symlinks(root: Path, rel: str) -> None:
    """Unlink symlinks at ``root/rel`` and at each of its parents below *root*."""
    parts = [part for part in rel.split("/") if part]
    for depth in range(1, len(parts) + 1):
        path = root.joinpath(*parts[:depth])
        if path.is_symlink():
            path.unlink()


def remove_symlinks_in_tree(top: Path) -> None:
    """Unlink every symlink below directory *top* (never descending through one)."""
    for dirpath, dirnames, filenames in os.walk(top):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            if path.is_symlink():
                path.unlink()
        dirnames[:] = [d for d in dirnames if (Path(dirpath) / d).is_dir()]
