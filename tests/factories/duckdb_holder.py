"""tests/factories/duckdb_holder.py

A second process that holds a DuckDB file open, so tests see DuckDB's real lock error text.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_HOLDER = (
    "import duckdb, os, sys\n"
    "conn = duckdb.connect(sys.argv[1])\n"
    "print(os.getpid(), flush=True)\n"
    "sys.stdin.read()\n"
)


def make_warehouse(path: Path) -> Path:
    """Create a small warehouse (one raw table) at *path* and return it."""
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS raw_test_csv")
        conn.execute("CREATE TABLE raw_test_csv.\"test_csv\" AS SELECT 1 AS id, 'a' AS name")
    finally:
        conn.close()
    return path


@contextmanager
def hold_warehouse(path: Path) -> Iterator[int]:
    """Hold *path* open for writing in another process; yield that process's pid."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        assert line.isdigit(), f"holder did not start: {line!r}"
        yield int(line)
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.wait(timeout=20)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()


def real_lock_error(path: Path, *, read_only: bool = False) -> tuple[Exception, int]:
    """Provoke DuckDB's real lock error on *path* from a second process; return (error, holder pid)."""
    import duckdb

    config = {"access_mode": "read_only"} if read_only else {}
    with hold_warehouse(path) as pid:
        try:
            duckdb.connect(str(path), config=config)
        except duckdb.IOException as exc:
            return exc, pid
    raise AssertionError("expected DuckDB to refuse the second connection")
