"""dango/utils/private_files.py

Apply an owner-only mode to an open file without requiring a POSIX-only API, and write a private file atomically.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


def apply_private_mode(fd: int, mode: int) -> None:
    """Set *mode* on the open file *fd* where the platform supports it.

    ``os.fchmod`` is applied exactly as before wherever it exists, and a real
    ``OSError`` from it propagates. Before Python 3.13 Windows has no
    ``os.fchmod`` and this function does nothing; on 3.13+ it exists there but
    only controls the read-only flag. Either way POSIX mode bits are not
    enforced on Windows, so the file is not protected by its mode: any
    protection comes from the ACLs it inherits from its directory, which may
    come from outside the user profile. This helper adds no protection there.
    """
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(fd, mode)


def write_private_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` atomically, readable by the owner only where modes apply.

    The text goes to a new file in the same folder (created ``0o600`` and never visible under
    another name once complete), is flushed and fsynced, and then replaces ``path`` with
    ``os.replace``: a failure at any step leaves the previous file untouched and removes the
    temporary one. Because the replacement is a new file, an existing file with looser
    permissions ends up ``0o600``. UTF-8 with ``\\n`` line endings. On Windows POSIX mode bits
    are not enforced (see :func:`apply_private_mode`); the file keeps the ACLs inherited from
    its folder, and nothing raises because of that.

    Raises:
        OSError: The folder is missing or not writable, or the replacement failed.
    """
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    opened = False
    try:
        apply_private_mode(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            opened = True
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        if not opened:
            os.close(fd)
        temporary.unlink(missing_ok=True)
        raise
