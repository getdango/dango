"""dango/cli/init_rollback.py

Snapshot of a project directory taken before `dango init`, and a rollback that removes only
what the init run created.
"""

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Pre-existing top-level directories init writes into; their contents are snapshotted so a
# rollback can tell the user's files from the ones this run added.
WRITE_DIRS = (".dango", "data", "custom_sources", "dbt", ".dlt", "metabase-plugins")

# Pre-existing top-level files up to this size are copied so a file init appended to or
# rewrote (for example `.gitignore`) can be put back byte for byte.
MAX_RESTORE_BYTES = 1 << 20


@dataclass
class InitSnapshot:
    """What existed in the project directory before init wrote anything."""

    top_level: frozenset[str]
    tree: frozenset[str]
    contents: dict[str, bytes] = field(default_factory=dict)


def _walk(base: Path, rel_dir: str) -> set[str]:
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(base / rel_dir):
        here = Path(dirpath).relative_to(base)
        found.update((here / name).as_posix() for name in (*dirnames, *filenames))
    return found


def take_snapshot(project_dir: Path) -> InitSnapshot:
    """Record the top-level entries, the contents of write directories and small files."""
    top_level = {p.name for p in project_dir.iterdir()} if project_dir.is_dir() else set()
    tree: set[str] = set()
    for name in WRITE_DIRS:
        path = project_dir / name
        if name in top_level and path.is_dir() and not path.is_symlink():
            tree |= _walk(project_dir, name)
    contents: dict[str, bytes] = {}
    for name in top_level:
        path = project_dir / name
        try:
            if (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_size <= MAX_RESTORE_BYTES
            ):
                contents[name] = path.read_bytes()
        except OSError:
            continue
    return InitSnapshot(frozenset(top_level), frozenset(tree), contents)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def rollback_to_snapshot(project_dir: Path, snapshot: InitSnapshot) -> list[str]:
    """Remove everything created since `snapshot`; return one line per action or failure."""
    lines: list[str] = []

    def drop(path: Path, rel: str) -> None:
        """Delete one created path, noting the result."""
        try:
            _remove(path)
            lines.append(f"Removed {rel}")
        except OSError as exc:
            lines.append(f"Could not remove {rel}: {exc}")

    for entry in sorted(project_dir.iterdir(), key=lambda p: p.name):
        if entry.name not in snapshot.top_level:
            drop(entry, entry.name)

    for name in WRITE_DIRS:
        base = project_dir / name
        if name not in snapshot.top_level or not base.is_dir() or base.is_symlink():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            here = Path(dirpath)
            for child in sorted((*dirnames, *filenames)):
                rel = (here / child).relative_to(project_dir).as_posix()
                if rel not in snapshot.tree:
                    drop(here / child, rel)
            dirnames[:] = [d for d in dirnames if (here / d).exists()]

    for name, data in snapshot.contents.items():
        path = project_dir / name
        try:
            if path.is_file() and path.read_bytes() != data:
                path.write_bytes(data)
                lines.append(f"Restored {name}")
        except OSError as exc:
            lines.append(f"Could not restore {name}: {exc}")
    return lines
