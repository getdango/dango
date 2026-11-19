"""dango/utils/safe_names.py

Checks for names that become one path component of something Dango deletes or writes.
"""

from __future__ import annotations

import re
from pathlib import Path

_PLAIN_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def validate_plain_name(name: str, what: str = "name") -> str:
    """Return *name* if it is a plain file-system-safe name, else raise ``ValueError``.

    Letters, digits, ``_``, ``-`` and ``.`` only, not starting with ``.`` or ``-`` and
    without ``..``, so it can never be empty, absolute, or a path with separators.
    """
    if not _PLAIN_NAME_RE.fullmatch(name) or ".." in name:
        raise ValueError(f"Unsafe {what} {name!r}: use only letters, digits, '_', '-' and '.'")
    return name


def contained_child(base: Path, name: str, what: str = "name") -> Path:
    """Return ``base / name``, refusing a name that is not a single path component.

    Rejects empty names, ``.``/``..``, separators and NUL bytes, so the result is always a
    direct child of *base*.
    """
    if not name or name in (".", "..") or any(c in name for c in ("/", "\\", "\x00")):
        raise ValueError(f"Unsafe {what} {name!r}: it must be a single path component")
    return base / name
