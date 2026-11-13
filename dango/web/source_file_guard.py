"""dango/web/source_file_guard.py

Decides whether a file may be deleted through a file source (CSV / local files).

Only the source's own input files qualify: right format, matching the source's file pattern, and
never the warehouse, its sidecar files or application state.
"""

from __future__ import annotations

import glob
from pathlib import Path

from fastapi import HTTPException

_DATABASE_SUFFIXES: frozenset[str] = frozenset({".duckdb", ".wal", ".db", ".sqlite"})


def _refuse(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)


def check_deletable_source_file(
    file_path: Path,
    *,
    source_dir: Path,
    file_pattern: str,
    project_root: Path,
    warehouse_path: Path,
) -> Path:
    """Return the resolved path of *file_path* if it may be deleted, else raise HTTP 403.

    ``source_dir`` and ``warehouse_path`` must already be resolved.
    """
    resolved = file_path.resolve()
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    warehouse_name = warehouse_path.name
    if (
        resolved == warehouse_path
        or resolved.name == warehouse_name
        or resolved.name.startswith(f"{warehouse_name}.")
        or resolved.suffix.lower() in _DATABASE_SUFFIXES
    ):
        raise _refuse("The warehouse database cannot be deleted through a source")

    state_dir = (project_root / ".dango").resolve()
    if resolved == state_dir or state_dir in resolved.parents:
        raise _refuse("Application state files cannot be deleted through a source")

    # Exactly what the loader reads: glob(directory / file_pattern) (not recursive unless the
    # pattern says so), in a format it can read.
    from dango.ingestion.csv_loader import SUPPORTED_READ_FUNCTIONS

    if resolved.suffix.lower() not in SUPPORTED_READ_FUNCTIONS:
        raise _refuse("This file type is not an input file of the source")
    loaded = {
        Path(match).resolve()
        for match in glob.glob(str(source_dir / file_pattern))
        if Path(match).suffix.lower() in SUPPORTED_READ_FUNCTIONS
    }
    if resolved not in loaded:
        raise _refuse("This file is not one the source loads (directory and file pattern)")

    return resolved
