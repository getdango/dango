"""dango/cli/init_rollback.py

Snapshot of a project directory taken before `dango init`, and a rollback that removes only
what the init run created.
"""

import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

# Pre-existing top-level directories init writes into; their contents are snapshotted so a
# rollback can tell the user's files from the ones this run added.
WRITE_DIRS = (".dango", "data", "custom_sources", "dbt", ".dlt", "metabase-plugins")

# Top-level files init itself writes. Only these are ever removed or restored, and only when
# the snapshot shows they did not exist (or, for restore, existed with other content).
INIT_FILES = (
    ".gitignore",
    ".pre-commit-config.yaml",
    "README.md",
    "CLAUDE.md",
    "AGENTS.md",
    "COMPATIBILITY.md",
    "SCALABILITY.md",
    "docker-compose.yml",
    "Dockerfile.metabase",
    "entrypoint.sh",
)
INIT_TOP_LEVEL = frozenset((*WRITE_DIRS, *INIT_FILES))

# Pre-existing files from INIT_FILES up to this size (inclusive) are copied, with their mode,
# so a file init appended to or rewrote (for example `.gitignore`) can be put back exactly.
MAX_RESTORE_BYTES = 1 << 20


@dataclass
class InitSnapshot:
    """What existed in the project directory before init wrote anything."""

    top_level: frozenset[str]
    tree: frozenset[str]
    contents: dict[str, tuple[bytes, int]] = field(default_factory=dict)


def _walk(base: Path, rel_dir: str) -> set[str]:
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(base / rel_dir):
        here = Path(dirpath).relative_to(base)
        found.update((here / name).as_posix() for name in (*dirnames, *filenames))
    return found


def take_snapshot(project_dir: Path) -> InitSnapshot:
    """Record top-level names, write-directory contents and the small files init may rewrite."""
    top_level = {p.name for p in project_dir.iterdir()} if project_dir.is_dir() else set()
    tree: set[str] = set()
    for name in WRITE_DIRS:
        path = project_dir / name
        if name in top_level and path.is_dir() and not path.is_symlink():
            tree |= _walk(project_dir, name)
    contents: dict[str, tuple[bytes, int]] = {}
    for name in INIT_FILES:
        path = project_dir / name
        try:
            if name in top_level and path.is_file() and not path.is_symlink():
                info = path.stat()
                if info.st_size <= MAX_RESTORE_BYTES:
                    contents[name] = (path.read_bytes(), stat.S_IMODE(info.st_mode))
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
        if entry.name in INIT_TOP_LEVEL and entry.name not in snapshot.top_level:
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

    for name, (data, mode) in snapshot.contents.items():
        path = project_dir / name
        try:
            if path.is_file() and (
                path.read_bytes() != data or stat.S_IMODE(path.stat().st_mode) != mode
            ):
                path.write_bytes(data)
                os.chmod(path, mode)
                lines.append(f"Restored {name}")
        except OSError as exc:
            lines.append(f"Could not restore {name}: {exc}")
    return lines
