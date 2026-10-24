"""dango/notebooks/safe_files.py

Symlink-safe file copying for notebook files.

``notebooks/`` can be writable by a lower-privileged user, so a path there may
be a symlink planted to redirect a read or a write. These helpers never follow
a symlink at the final path component, and optionally refuse symlinked
directory components below a given root.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import stat
from pathlib import Path

DEFAULT_MODE = 0o644


class UnsafeFileError(OSError):
    """Raised when a source or destination path is a symlink or not a regular file."""


def _check_components(path: Path, root: Path) -> None:
    """Refuse symlinked directory components of ``path`` strictly below ``root``."""
    try:
        parts = path.parent.relative_to(root).parts
    except ValueError as exc:
        raise UnsafeFileError(errno.EPERM, "Path is outside the allowed root") from exc
    current = root
    for part in parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise UnsafeFileError(errno.ELOOP, "Path contains a symbolic link")


def copy_regular_file(
    src: Path,
    dst: Path,
    *,
    mode: int = DEFAULT_MODE,
    src_root: Path | None = None,
    dst_root: Path | None = None,
) -> None:
    """Copy a regular file without following symlinks.

    Args:
        src: Source file. Must be a regular file (not a symlink, FIFO or device).
        dst: Destination file. Must not exist; a dangling symlink counts as existing.
        mode: Permission bits set explicitly on the new file.
        src_root: If given, symlinked directory components of ``src`` below
            this directory are refused.
        dst_root: Same, for ``dst``.

    Raises:
        FileNotFoundError: ``src`` does not exist.
        UnsafeFileError: ``src`` is not a regular file, or a path contains a symlink.
        FileExistsError: ``dst`` already exists (including as a symlink).
    """
    if src_root is not None:
        _check_components(src, src_root)
    if dst_root is not None:
        _check_components(dst, dst_root)

    if not stat.S_ISREG(os.lstat(src).st_mode):
        raise UnsafeFileError(errno.EPERM, "Source is not a regular file")

    try:
        src_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeFileError(errno.ELOOP, "Source is a symbolic link") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(src_fd).st_mode):
            raise UnsafeFileError(errno.EPERM, "Source is not a regular file")
        dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            with os.fdopen(src_fd, "rb", closefd=False) as fsrc, os.fdopen(dst_fd, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
                fdst.flush()
                os.fchmod(fdst.fileno(), mode)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(dst)
            raise
    finally:
        os.close(src_fd)
