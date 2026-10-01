"""tests/unit/test_local_files_all_deleted_protection.py

1.0.10-M17: local_files/csv sync must not empty the table when every source file is gone (real DuckDB).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import duckdb
import pytest

from dango.config.models import (
    CSVSourceConfig,
    DataSource,
    LocalFilesSourceConfig,
    SourceType,
)
from dango.ingestion.csv_loader import CSVLoader
from dango.ingestion.dlt_runner import EMPTY_REPLACE_PROTECTION_ERROR_TYPE, DltPipelineRunner
from dango.utils.sync_history import load_sync_history

NAME = "orders"
TABLE = f"raw_{NAME}.{NAME}"


def _write(path: Path, rows: list[str], header: str = "id,item") -> Path:
    path.write_text(header + "\n" + "\n".join(rows) + ("\n" if rows else ""))
    return path


def _local_source(tmp_path: Path) -> DataSource:
    return DataSource(
        name=NAME,
        type=SourceType.LOCAL_FILES,
        local_files=LocalFilesSourceConfig(directory=tmp_path / "uploads", file_pattern="*.csv"),
    )


def _csv_source(tmp_path: Path) -> DataSource:
    return DataSource(
        name=NAME,
        type=SourceType.CSV,
        csv=CSVSourceConfig(directory=tmp_path / "uploads", file_pattern="*.csv"),
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


def _sync(runner, source, **kw):
    fn = runner._run_csv_source if source.type == SourceType.CSV else runner._run_local_files_source
    return fn(source, **kw)


def _seeded(tmp_path, uploads, runner, source_factory=_local_source):
    f = _write(uploads / "orders.csv", ["1,a", "2,b"])
    src = source_factory(tmp_path)
    assert _sync(runner, src)["status"] == "success"
    assert _count(runner) == 2
    return f, src


def test_all_files_deleted_blocked_and_rows_preserved(tmp_path, uploads, runner):
    f, src = _seeded(tmp_path, uploads, runner)
    before = _metadata(runner)
    f.unlink()

    result = _sync(runner, src, allow_empty_replace=False)

    assert result["status"] == "failed"
    assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
    assert "--allow-empty-replace" in result["error"]
    assert _count(runner) == 2
    assert _metadata(runner) == before


def test_all_files_deleted_allowed_with_flag(tmp_path, uploads, runner):
    f, src = _seeded(tmp_path, uploads, runner)
    f.unlink()

    result = _sync(runner, src, allow_empty_replace=True)

    assert result["status"] == "success"
    assert result["deleted"] == 1
    assert _count(runner) == 0


def test_one_of_two_files_deleted_removes_only_that_file(tmp_path, uploads, runner):
    _write(uploads / "a.csv", ["1,a", "2,b"])
    b = _write(uploads / "b.csv", ["3,c"])
    src = _local_source(tmp_path)
    assert _sync(runner, src)["status"] == "success"
    assert _count(runner) == 3
    b.unlink()

    result = _sync(runner, src, allow_empty_replace=False)

    assert result["status"] == "success"
    assert result["deleted"] == 1
    assert _query(runner, f"SELECT id FROM {TABLE} ORDER BY id") == [(1,), (2,)]


def test_file_returns_after_block_resumes_normally(tmp_path, uploads, runner):
    f, src = _seeded(tmp_path, uploads, runner)
    content = f.read_text()
    f.unlink()
    assert _sync(runner, src)["status"] == "failed"

    f.write_text(content)
    os.utime(f, (time.time() + 5, time.time() + 5))
    result = _sync(runner, src)

    assert result["status"] == "success"
    assert _count(runner) == 2
    assert _query(runner, f"SELECT COUNT(DISTINCT id) FROM {TABLE}")[0][0] == 2


def test_csv_source_runner_blocked_too(tmp_path, uploads, runner):
    f, src = _seeded(tmp_path, uploads, runner, _csv_source)
    before = _metadata(runner)
    f.unlink()

    result = _sync(runner, src, allow_empty_replace=False)

    assert result["status"] == "failed"
    assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
    assert _count(runner) == 2
    assert _metadata(runner) == before

    assert _sync(runner, src, allow_empty_replace=True)["status"] == "success"
    assert _count(runner) == 0


def test_full_refresh_with_missing_file_still_protected(tmp_path, uploads, runner):
    f, src = _seeded(tmp_path, uploads, runner)
    f.unlink()

    result = _sync(runner, src, full_refresh=True, allow_empty_replace=False)

    assert result["status"] == "failed"
    assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
    assert _count(runner) == 2


def test_first_sync_empty_directory_unchanged(tmp_path, uploads, runner):
    result = _sync(runner, _local_source(tmp_path), allow_empty_replace=False)

    assert result["status"] == "error"
    assert "No files found to load" in result["error"]
    assert result.get("error_type") != EMPTY_REPLACE_PROTECTION_ERROR_TYPE


def test_remaining_file_with_zero_rows_blocked(tmp_path, uploads, runner):
    """Case 2: the original file is gone and the only new file has 0 rows."""
    f, src = _seeded(tmp_path, uploads, runner)
    f.unlink()
    _write(uploads / "other.csv", [])

    result = _sync(runner, src, allow_empty_replace=False)

    assert result["status"] == "failed"
    assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
    assert _count(runner) == 2
    assert [m[3] for m in _metadata(runner)] == ["loaded"]

    allowed = _sync(runner, src, allow_empty_replace=True)
    assert allowed["status"] == "success"
    assert _count(runner) == 0


def test_replacing_file_with_new_nonempty_file_allowed(tmp_path, uploads, runner):
    f, src = _seeded(tmp_path, uploads, runner)
    f.unlink()
    _write(uploads / "other.csv", ["9,z"])

    result = _sync(runner, src, allow_empty_replace=False)

    assert result["status"] == "success"
    assert _query(runner, f"SELECT id FROM {TABLE}") == [(9,)]


def test_loader_default_is_protected(tmp_path, uploads, runner):
    f, src = _seeded(tmp_path, uploads, runner)
    f.unlink()

    result = CSVLoader(tmp_path, runner.duckdb_path).load(
        NAME, src.local_files, target_schema=f"raw_{NAME}"
    )

    assert result["status"] == "failed"
    assert result["total_rows"] == 2
    assert _count(runner) == 2


def test_sync_history_records_block(tmp_path, uploads, runner):
    f, src = _seeded(tmp_path, uploads, runner)
    f.unlink()

    result = runner.run_source(src, allow_empty_replace=False)

    assert result["status"] == "failed"
    assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
    latest = load_sync_history(tmp_path, NAME)[0]
    assert latest["status"] == "failed"
    assert "--allow-empty-replace" in latest["error_message"]
    assert _count(runner) == 2


def test_policy_allow_via_run_source(tmp_path, uploads, runner):
    f, src = _seeded(tmp_path, uploads, runner)
    src.empty_sync_policy = "allow"
    f.unlink()

    result = runner.run_source(src)

    assert result["status"] == "success"
    assert _count(runner) == 0


def test_same_file_returning_after_allowed_delete_reloads(tmp_path, uploads, runner):
    """Moving the identical file back (mtime unchanged) after rows were removed reloads it."""
    f, src = _seeded(tmp_path, uploads, runner)
    away = tmp_path / "orders.csv.away"
    f.rename(away)
    assert _sync(runner, src, allow_empty_replace=True)["status"] == "success"
    assert _count(runner) == 0

    away.rename(f)
    result = _sync(runner, src)

    assert result["status"] == "success"
    assert result["new"] == 1
    assert _count(runner) == 2
