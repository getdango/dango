"""tests/factories/partial_file_source.py

Files and sources for the WP-67 tests: valid CSVs and a Windows-style non-UTF-8 export.
"""

from __future__ import annotations

import os
from pathlib import Path

from dango.config.models import (
    CSVSourceConfig,
    DataSource,
    LocalFilesSourceConfig,
    SourceType,
)
from dango.ingestion.dlt_runner import DltPipelineRunner

NAME = "sales"
SCHEMA = f"raw_{NAME}"


def write_good(path: Path, rows: list[str]) -> Path:
    """A valid UTF-8 file with columns id,item."""
    path.write_text("id,item\n" + "\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    return path


def write_latin1(path: Path, first_id: int = 100) -> Path:
    """A typical Excel "CSV (Comma delimited)" export: one Latin-1 byte far below the header.

    DuckDB samples the top of the file as UTF-8 and trips on the byte when it reads the rest.
    """
    lines = ["id,item"] + [f"{first_id + i},x" for i in range(30000)]
    lines.append(f"{first_id + 30000},caf\xe9")
    path.write_bytes(("\n".join(lines) + "\n").encode("latin-1"))
    return path


def bump_mtime(path: Path, seconds: int = 120) -> None:
    """Make *path* look modified later than the load that recorded it."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + seconds))


def make_source(kind: str, directory: Path, name: str = NAME) -> DataSource:
    """A csv or local_files source over every *.csv in *directory*."""
    if kind == "csv":
        csv = CSVSourceConfig(directory=directory, file_pattern="*.csv")
        return DataSource(name=name, type=SourceType.CSV, csv=csv)
    files = LocalFilesSourceConfig(directory=directory, file_pattern="*.csv")
    return DataSource(name=name, type=SourceType.LOCAL_FILES, local_files=files)


def run_method(runner: DltPipelineRunner, source: DataSource, **kwargs):
    """Call the file-source method that matches *source* (both are tested alike)."""
    if source.type == SourceType.CSV:
        return runner._run_csv_source(source, **kwargs)
    return runner._run_local_files_source(source, **kwargs)
