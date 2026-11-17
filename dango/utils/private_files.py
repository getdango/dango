"""dango/utils/private_files.py

Apply an owner-only mode to an open file without requiring a POSIX-only API.
"""

from __future__ import annotations

import os


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
