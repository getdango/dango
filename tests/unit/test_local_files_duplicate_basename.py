"""tests/unit/test_local_files_duplicate_basename.py

1.0.10-M19: local_files/csv syncs must refuse files that share a file name (rows are keyed by
name, so same-named files in different folders silently overwrote/deleted each other). Real DuckDB.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import duckdb
import pytest

from dango.config.models import DataSource, LocalFilesSourceConfig, SourceType
from dango.ingestion.csv_loader import DUPLICATE_FILENAME_ERROR_TYPE
from dango.ingestion.dlt_runner import DltPipelineRunner

NAME = "sales"
TABLE = f"raw_{NAME}.{NAME}"


def _write(path: Path, rows: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("id,item\n" + "\n".join(rows) + "\n")
    return path


def _bump(path: Path) -> None:
    future = time.time() + 10
    os.utime(path, (future, future))


def _source(tmp_path: Path, pattern: str) -> DataSource:
    return DataSource(
        name=NAME,
        type=SourceType.LOCAL_FILES,
        local_files=LocalFilesSourceConfig(directory=tmp_path / "uploads", file_pattern=pattern),
    )


@pytest.fixture
def uploads(tmp_path: Path) -> Path:
    d = tmp_path / "uploads"
    d.mkdir()
    return d


@pytest.fixture
def runner(tmp_path: Path) -> DltPipelineRunner:
    return DltPipelineRunner(tmp_path)


def _query(runner: DltPipelineRunner, sql: str) -> list[tuple]:
    conn = duckdb.connect(str(runner.duckdb_path), config={"access_mode": "read_only"})
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _count(runner: DltPipelineRunner) -> int:
    return _query(runner, f"SELECT COUNT(*) FROM {TABLE}")[0][0]


def _metadata(runner: DltPipelineRunner) -> list[tuple]:
    return _query(
        runner,
        "SELECT file_path, file_mtime, rows_loaded, status FROM _dango_file_metadata "
        "ORDER BY file_path",
    )


def test_duplicate_basenames_refused_no_writes(tmp_path, uploads, runner):
    _write(uploads / "2026-09" / "sales.csv", ["1,a", "2,b"])
    _write(uploads / "2026-10" / "sales.csv", ["3,c", "4,d", "5,e"])
    src = _source(tmp_path, "*/sales.csv")

    result = runner._run_local_files_source(src)

    assert result["status"] == "failed"
    assert result["error_type"] == DUPLICATE_FILENAME_ERROR_TYPE
    assert os.path.join("2026-09", "sales.csv") in result["error"]
    assert os.path.join("2026-10", "sales.csv") in result["error"]
    assert "unique file names" in result["error"]
    assert not runner.duckdb_path.exists() or not _query(
        runner,
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'sales'",
    )
    if runner.duckdb_path.exists():
        tables = _query(runner, "SELECT table_name FROM information_schema.tables")
        assert ("_dango_file_metadata",) not in tables


def test_duplicate_basenames_refused_on_resync_preserves_rows(tmp_path, uploads, runner):
    _write(uploads / "2026-09" / "a.csv", ["1,a", "2,b"])
    _write(uploads / "2026-10" / "b.csv", ["3,c"])
    src = _source(tmp_path, "*/*.csv")
    assert runner._run_local_files_source(src)["status"] == "success"
    assert _count(runner) == 3
    before = _metadata(runner)

    _write(uploads / "2026-10" / "a.csv", ["9,z"])
    result = runner._run_local_files_source(src)

    assert result["status"] == "failed"
    assert os.path.join("2026-09", "a.csv") in result["error"]
    assert _count(runner) == 3
    assert _metadata(runner) == before
    assert _query(runner, f"SELECT id FROM {TABLE} ORDER BY id") == [(1,), (2,), (3,)]


def test_unique_basenames_in_subfolders_still_work(tmp_path, uploads, runner):
    a = _write(uploads / "2026-09" / "sep.csv", ["1,a", "2,b"])
    b = _write(uploads / "2026-10" / "oct.csv", ["3,c", "4,d", "5,e"])
    src = _source(tmp_path, "*/*.csv")
    assert runner._run_local_files_source(src)["status"] == "success"
    assert _count(runner) == 5

    a.write_text("id,item\n1,a\n2,b\n6,f\n")
    _bump(a)
    result = runner._run_local_files_source(src)
    assert result["status"] == "success"
    assert result["updated"] == 1
    assert _count(runner) == 6

    b.unlink()
    result = runner._run_local_files_source(src)
    assert result["status"] == "success"
    assert _query(runner, f"SELECT DISTINCT _dango_filename FROM {TABLE}") == [("sep.csv",)]
    assert _count(runner) == 3


def test_moved_file_old_path_gone_is_not_blocked(tmp_path, uploads, runner):
    old = _write(uploads / "inbox" / "sales.csv", ["1,a", "2,b"])
    src = _source(tmp_path, "*/sales.csv")
    assert runner._run_local_files_source(src)["status"] == "success"

    new = uploads / "archive" / "sales.csv"
    new.parent.mkdir()
    old.rename(new)
    result = runner._run_local_files_source(src)

    assert result["status"] == "success"
    assert _count(runner) == 2


def test_previously_loaded_file_still_present_with_same_name_is_blocked(tmp_path, uploads, runner):
    _write(uploads / "inbox" / "sales.csv", ["1,a", "2,b"])
    src = _source(tmp_path, "inbox/*.csv")
    assert runner._run_local_files_source(src)["status"] == "success"
    before = _metadata(runner)

    # pattern now matches only a different folder, but the old file still exists on disk
    _write(uploads / "archive" / "sales.csv", ["7,x"])
    result = runner._run_local_files_source(_source(tmp_path, "archive/*.csv"))

    assert result["status"] == "failed"
    assert result["error_type"] == DUPLICATE_FILENAME_ERROR_TYPE
    assert _count(runner) == 2
    assert _metadata(runner) == before


def test_error_reaches_run_source_unchanged_with_full_refresh(tmp_path, uploads, runner):
    _write(uploads / "2026-09" / "x.csv", ["1,a", "2,b"])
    src = _source(tmp_path, "*/*.csv")
    assert runner._run_local_files_source(src)["status"] == "success"
    before = _metadata(runner)

    _write(uploads / "2026-10" / "x.csv", ["3,c"])
    result = runner._run_local_files_source(src, full_refresh=True)

    assert result["status"] == "failed"
    assert result["error_type"] == DUPLICATE_FILENAME_ERROR_TYPE
    assert "unique file names" in result["error"]
    assert "--allow-empty-replace" not in result["error"]
    # full refresh backed the table up; it must be restored with its metadata
    assert _count(runner) == 2
    assert _metadata(runner) == before


def test_moved_file_with_other_files_keeps_every_row(tmp_path, uploads, runner):
    old = _write(uploads / "inbox" / "sales.csv", ["1,a", "2,b"])
    _write(uploads / "other" / "extra.csv", ["3,c"])
    src = _source(tmp_path, "*/*.csv")
    assert runner._run_local_files_source(src)["status"] == "success"

    new = uploads / "archive" / "sales.csv"
    new.parent.mkdir()
    old.rename(new)
    result = runner._run_local_files_source(src)

    assert result["status"] == "success"
    assert _query(runner, f"SELECT id FROM {TABLE} ORDER BY id") == [(1,), (2,), (3,)]


def test_moved_file_stays_intact_on_later_syncs(tmp_path, uploads, runner):
    old = _write(uploads / "a" / "x.csv", ["1,a", "2,b"])
    src = _source(tmp_path, "*/*.csv")
    assert runner._run_local_files_source(src)["status"] == "success"
    new = uploads / "b" / "x.csv"
    new.parent.mkdir()
    old.rename(new)
    for _ in range(3):
        runner._run_local_files_source(src)
        assert _count(runner) == 2


def test_moved_file_replaced_by_header_only_copy_is_not_silently_emptied(tmp_path, uploads, runner):
    old = _write(uploads / "a" / "x.csv", ["1,a", "2,b"])
    src = _source(tmp_path, "*/*.csv")
    assert runner._run_local_files_source(src)["status"] == "success"
    old.unlink()
    _write(uploads / "b" / "x.csv", [])

    result = runner._run_local_files_source(src)

    assert result["status"] != "success"
    assert _count(runner) == 2


def test_full_refresh_allow_empty_replace_refusal_keeps_table(tmp_path, uploads, runner):
    _write(uploads / "2026-09" / "x.csv", ["1,a", "2,b"])
    src = _source(tmp_path, "*/*.csv")
    assert runner._run_local_files_source(src)["status"] == "success"
    before = _metadata(runner)
    _write(uploads / "2026-10" / "x.csv", ["3,c"])

    result = runner._run_local_files_source(src, full_refresh=True, allow_empty_replace=True)

    assert result["status"] == "failed"
    assert result["error_type"] == DUPLICATE_FILENAME_ERROR_TYPE
    assert _count(runner) == 2
    assert _metadata(runner) == before
