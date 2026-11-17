"""dango/utils/private_files.py

Apply an owner-only mode to an open file without requiring a POSIX-only API.
"""

from __future__ import annotations

import os


def apply_private_mode(fd: int, mode: int) -> None:
    """Set *mode* on the open file *fd* where the platform supports it.

    ``os.fchmod`` exists on macOS and Linux and is applied exactly as before; a
    real ``OSError`` from it propagates. It does not exist on Windows, where
    this function does nothing. On Windows POSIX mode bits are not enforced, so
    the file is protected only by the default ACLs of the user profile it lives
    in; this helper does not add any protection there.
    """
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(fd, mode)
