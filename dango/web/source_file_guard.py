"""dango/web/source_file_guard.py

Decides whether a file may be deleted through a file source (CSV / local files).

Only the source's own input files qualify: right format, matching the source's file pattern, and
never the warehouse, its sidecar files or application state.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from fastapi import HTTPException

# Formats a local_files source loads; a csv source loads only .csv.
LOCAL_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {".csv", ".json", ".jsonl", ".ndjson", ".parquet"}
)
_DATABASE_SUFFIXES: frozenset[str] = frozenset({".duckdb", ".wal", ".db", ".sqlite"})


def _refuse(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)


def check_deletable_source_file(
    file_path: Path,
    *,
    source_dir: Path,
    source_type: str,
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

    allowed = LOCAL_FILE_EXTENSIONS if source_type == "local_files" else frozenset({".csv"})
    if resolved.suffix.lower() not in allowed:
        raise _refuse("This file type is not an input file of the source")

    relative = resolved.relative_to(source_dir).as_posix()
    if not (
        fnmatch.fnmatch(relative, file_pattern) or fnmatch.fnmatch(resolved.name, file_pattern)
    ):
        raise _refuse("This file does not match the source's file pattern")

    return resolved
