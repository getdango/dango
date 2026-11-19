"""dango/utils/safe_names.py

Check that a name is a single path component before Dango deletes or writes under it.
"""

from __future__ import annotations

import re
from pathlib import Path

_BACKUP_SHAPED = re.compile(r".+_backup_\d{8}_\d{6}")


def contained_child(base: Path, name: str, what: str = "name") -> Path:
    """Return ``base / name``, refusing a name that is not a single path component.

    Rejects empty names, ``.``/``..``, ``/``, ``\\``, ``:`` (drive-relative names such as
    ``D:x`` on Windows) and NUL bytes. Any other character (spaces, non-ASCII letters) is
    allowed. It does not check Windows reserved device names.
    """
    if not name or name in (".", "..") or any(c in name for c in ("/", "\\", ":", "\x00")):
        raise ValueError(f"Unsafe {what} {name!r}: it must be a single path component")
    return base / name


def reject_backup_shaped_name(name: str, what: str = "name") -> str:
    """Return *name*, or raise ``ValueError`` if it ends like a dlt state backup directory.

    ``<x>_backup_<YYYYMMDD>_<HHMMSS>`` directories hold copies of pipeline state, so a
    pipeline or source with such a name could be mistaken for a backup of ``<x>``.
    """
    if _BACKUP_SHAPED.fullmatch(name):
        raise ValueError(
            f"The {what} {name!r} is reserved: names ending in _backup_YYYYMMDD_HHMMSS are "
            "used for pipeline state backups. Choose another name."
        )
    return name
