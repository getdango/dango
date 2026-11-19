"""dango/utils/safe_names.py

Check that a name is a single path component before Dango deletes or writes under it.
"""

from __future__ import annotations

from pathlib import Path


def contained_child(base: Path, name: str, what: str = "name") -> Path:
    """Return ``base / name``, refusing a name that is not a single path component.

    Rejects empty names, ``.``/``..``, separators and NUL bytes, so the result is always a
    direct child of *base*. Any other character (spaces, non-ASCII letters) is allowed.
    """
    if not name or name in (".", "..") or any(c in name for c in ("/", "\\", "\x00")):
        raise ValueError(f"Unsafe {what} {name!r}: it must be a single path component")
    return base / name
