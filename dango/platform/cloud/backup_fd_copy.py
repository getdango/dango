"""dango/platform/cloud/backup_fd_copy.py

Descriptor-pinned copies for the root scheduled backup.

The project directory belongs to the service user, who can swap a directory for a
link at any moment.  A pathname check followed by a copy that reopens the path by
name (``shutil.copytree``) can therefore be redirected to a root-only target.  These
helpers open the top directory once with ``O_NOFOLLOW`` and then open every member
relative to the descriptor already held, again without following links, so the
tree that is read is the one that was opened whatever happens to the paths.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)


def open_dir_beneath(root: Path, rel: str = "") -> int:
    """Open ``root/rel`` as a directory descriptor, following no link at any component.

    *root* itself is opened without following a final-component link; each part of *rel*
    is then opened relative to its parent's descriptor.  The caller closes the result.
    """
    fd = os.open(root, DIR_FLAGS)
    try:
        for part in [p for p in rel.split("/") if p]:
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_owned_dir(path: Path, uid: int) -> int:
    """Open directory *path* once (a link is followed here, and only here) and check its owner.

    The owner is read from the open descriptor, so what is checked is exactly what the caller
    walks afterwards; nothing reopens the path.  Raises ``OSError`` if *path* is not a
    directory or its owner is not *uid*.  The caller closes the result.
    """
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC)
    try:
        if os.fstat(fd).st_uid != uid:
            raise PermissionError(f"not owned by uid {uid}: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _copy_open_file(fd: int, name: str, dest: Path) -> None:
    """Copy the regular file open as *fd* to *dest* (mode kept minus setuid/setgid/sticky)."""
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"not a regular file: {name}")
        with open(dest, "wb") as out:
            shutil.copyfileobj(handle, out, 1024 * 1024)
    os.chmod(dest, stat.S_IMODE(info.st_mode) & 0o777)


def copy_file_beneath(root: Path, rel: str, dest: Path) -> None:
    """Copy regular file ``root/rel`` to *dest*, following no link in any component.

    Raises ``OSError`` if any component is a link or the final one is not a regular file.
    """
    parent, _, name = rel.rpartition("/")
    dir_fd = open_dir_beneath(root, parent)
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    _copy_open_file(fd, rel, dest)


def _walk(
    dir_fd: int,
    dest: Path,
    rel: str,
    left_out: list[str],
    label: str,
    excluded: Callable[[str], bool],
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in os.listdir(dir_fd):
        if excluded(name):
            continue
        member = f"{rel}/{name}" if rel else name
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
        if stat.S_ISREG(mode):
            _copy_open_file(os.open(name, _FILE_FLAGS, dir_fd=dir_fd), member, dest / name)
        elif stat.S_ISDIR(mode):
            child_fd = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
            try:
                _walk(child_fd, dest / name, member, left_out, label, excluded)
            finally:
                os.close(child_fd)
        else:
            left_out.append(f"{label}/{member}")
    # Directory modes are applied last (a read-only source directory must stay writable
    # while it is filled), without setuid/setgid/sticky.
    os.chmod(dest, stat.S_IMODE(os.fstat(dir_fd).st_mode) & 0o777)


def copy_tree_pinned(
    root_fd: int,
    dest: Path,
    *,
    left_out: list[str],
    label: str,
    excluded: Callable[[str], bool],
) -> None:
    """Copy the directory open as *root_fd* into *dest*: regular files and directories only.

    Links and special files are never followed or read; each is recorded in *left_out* as
    ``label/<path>``.  Names for which *excluded* is true are left out.  No copy keeps
    setuid/setgid/sticky bits.  Raises ``OSError`` if a member was swapped for a link.
    """
    _walk(root_fd, dest, "", left_out, label, excluded)
