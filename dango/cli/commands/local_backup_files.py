"""dango/cli/commands/local_backup_files.py

Helpers for the local ``dango backup`` commands: which archive members a restore writes, where they land, and how a safety backup adds a directory tree.
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

from rich.markup import escape

from dango.cli import console
from dango.platform.cloud.backup import (
    BACKUP_DIRS,
    BACKUP_FILES,
    DATABASE_FILES,
    METABASE_CONFIG_FILE,
    wal_sidecar_names,
)
from dango.platform.cloud.backup_restore import is_backup_excluded

SRV_PREFIX = "/srv/dango/project/"


def add_tree(tf: tarfile.TarFile, root: Path, arcname: str) -> None:
    """Add regular files and directories below *root* as *arcname*/…, never following links.

    ``BACKUP_EXCLUDES`` names are left out; links and special files are left out with a
    warning (a restore refuses archives that contain them).
    """
    tf.add(str(root), arcname=arcname, recursive=False)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        prefix = arcname if rel_dir == "." else f"{arcname}/{rel_dir}"
        kept_dirs = []
        for name in sorted(dirnames):
            path = Path(dirpath) / name
            if is_backup_excluded(name):
                continue
            if path.is_symlink():
                console.print(
                    f"[yellow]Warning:[/yellow] Not in safety backup (link): {escape(f'{prefix}/{name}')}"
                )
                continue
            tf.add(str(path), arcname=f"{prefix}/{name}", recursive=False)
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if is_backup_excluded(name):
                continue
            if path.is_symlink() or not path.is_file():
                console.print(
                    "[yellow]Warning:[/yellow] Not in safety backup (link or special file): "
                    f"{escape(f'{prefix}/{name}')}"
                )
                continue
            tf.add(str(path), arcname=f"{prefix}/{name}", recursive=False)


def clear_sidecar(sidecar: Path, keep_suffix: str) -> None:
    """Delete a live WAL sidecar, or rename it with *keep_suffix* when one is given."""
    if not keep_suffix:
        sidecar.unlink(missing_ok=True)
    elif sidecar.exists():
        kept = sidecar.with_name(sidecar.name + keep_suffix)
        sidecar.replace(kept)
        console.print(f"[yellow]Warning:[/yellow] Live database WAL kept as {kept}")


def member_rel_path(name: str) -> str:
    """Map an archive member name to a path relative to the project root."""
    if name.startswith(SRV_PREFIX):
        return name[len(SRV_PREFIX) :]
    if name.startswith("backup-") and "/" in name:
        # Legacy path: backup-TIMESTAMP/project/...
        return name.split("/", 1)[1]
    return name


DB_SIDECARS = frozenset(side for db in DATABASE_FILES for side in wal_sidecar_names(db))


def is_backup_member(rel_path: str) -> bool:
    """True if *rel_path* is something a Dango backup contains (secrets never are)."""
    if rel_path in ("manifest.json", METABASE_CONFIG_FILE) or rel_path in BACKUP_FILES:
        return True
    if rel_path in DB_SIDECARS:  # carried by a safety backup when a checkpoint failed
        return True
    return any(rel_path.startswith(d + "/") for d in BACKUP_DIRS)
