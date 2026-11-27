"""tests/unit/test_file_source_atomic_metadata.py

WP-66: a csv / local_files file's rows and its bookkeeping row commit together (real DuckDB).
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from dango.config.models import (
    CSVSourceConfig,
    DataSource,
    LocalFilesSourceConfig,
    SourceType,
)
from dango.ingestion import dlt_runner
from dango.ingestion.csv_loader import CSVLoader
from dango.ingestion.dlt_runner import DltPipelineRunner

NAME = "orders"
SCHEMA = f"raw_{NAME}"
METADATA_SQL = "INSERT OR REPLACE INTO _dango_file_metadata"


@pytest.fixture(params=["csv", "local_files"])
def kind(request) -> str:
    return request.param


@pytest.fixture
def uploads(tmp_path: Path) -> Path:
    d = tmp_path / "uploads"
    d.mkdir()
    return d


@pytest.fixture
def runner(tmp_path: Path) -> DltPipelineRunner:
    return DltPipelineRunner(tmp_path)


def _write(path: Path, rows: list[str]) -> Path:
    path.write_text("id,item\n" + "\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    return path


def _source(kind: str, tmp_path: Path) -> DataSource:
    if kind == "csv":
        cfg = CSVSourceConfig(directory=tmp_path / "uploads", file_pattern="*.csv")
        return DataSource(name=NAME, type=SourceType.CSV, csv=cfg)
    cfg = LocalFilesSourceConfig(directory=tmp_path / "uploads", file_pattern="*.csv")
    return DataSource(name=NAME, type=SourceType.LOCAL_FILES, local_files=cfg)


def _sync(runner: DltPipelineRunner, src: DataSource, **kw):
    fn = runner._run_csv_source if src.type == SourceType.CSV else runner._run_local_files_source
    return fn(src, **kw)


def _query(runner: DltPipelineRunner, sql: str) -> list[tuple]:
    conn = duckdb.connect(str(runner.duckdb_path), config={"access_mode": "read_only"})
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _rows(runner: DltPipelineRunner) -> list[tuple]:
    return _query(runner, f"SELECT id, item FROM {SCHEMA}.{NAME} ORDER BY id")


def _recorded_files(runner: DltPipelineRunner) -> list[str]:
    paths = _query(runner, "SELECT file_path FROM _dango_file_metadata ORDER BY file_path")
    return [Path(p[0]).name for p in paths]


class _FailingMetadataConnection:
    """Delegates to a real connection; the metadata INSERT of one file raises *error*."""

    def __init__(self, conn, filename: str, error: BaseException) -> None:
        self._conn = conn
        self._filename = filename
        self._error = error

    def execute(self, sql, params=None):
        if METADATA_SQL in sql and params and str(params[1]).endswith(self._filename):
            raise self._error
        return self._conn.execute(sql) if params is None else self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _fail_metadata_of(monkeypatch: pytest.MonkeyPatch, filename: str, error: BaseException) -> None:
    real_connect = dlt_runner._connect_with_lock_retry

    def connect(*args, **kwargs):
        return _FailingMetadataConnection(real_connect(*args, **kwargs), filename, error)

    monkeypatch.setattr(dlt_runner, "_connect_with_lock_retry", connect)


def _seed_two_files(kind: str, tmp_path: Path, uploads: Path, runner: DltPipelineRunner):
    _write(uploads / "a.csv", ["1,a", "2,b"])
    _write(uploads / "b.csv", ["3,c"])
    src = _source(kind, tmp_path)
    assert _sync(runner, src)["status"] == "success"
    return src


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RuntimeError("disk trouble")])
def test_failure_between_rows_and_metadata_leaves_neither(
    kind, error, tmp_path, uploads, runner, monkeypatch
):
    src = _seed_two_files(kind, tmp_path, uploads, runner)
    _write(uploads / "c.csv", ["4,d", "5,e"])
    _fail_metadata_of(monkeypatch, "c.csv", error)

    if isinstance(error, KeyboardInterrupt):
        assert _sync(runner, src)["status"] == "interrupted"
    else:
        assert _sync(runner, src)["new"] == 0  # the file counts as failed, as before

    assert _rows(runner) == [(1, "a"), (2, "b"), (3, "c")]
    assert _recorded_files(runner) == ["a.csv", "b.csv"]


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RuntimeError("disk trouble")])
def test_next_sync_loads_the_file_exactly_once(kind, error, tmp_path, uploads, runner, monkeypatch):
    src = _seed_two_files(kind, tmp_path, uploads, runner)
    _write(uploads / "c.csv", ["4,d", "5,e"])
    with monkeypatch.context() as patched:
        _fail_metadata_of(patched, "c.csv", error)
        _sync(runner, src)

    result = _sync(runner, src)

    assert result["status"] == "success" and result["new"] == 1
    assert _rows(runner) == [(1, "a"), (2, "b"), (3, "c"), (4, "d"), (5, "e")]
    assert _recorded_files(runner) == ["a.csv", "b.csv", "c.csv"]


def test_the_first_file_of_a_new_source_is_atomic_too(kind, tmp_path, uploads, runner, monkeypatch):
    _write(uploads / "a.csv", ["1,a"])
    _fail_metadata_of(monkeypatch, "a.csv", KeyboardInterrupt())

    assert _sync(runner, _source(kind, tmp_path))["status"] == "interrupted"

    assert _rows(runner) == []
    assert _recorded_files(runner) == []


# Controls: green before and after the change.


def test_control_ordinary_sync_loads_each_file_once(kind, tmp_path, uploads, runner):
    src = _seed_two_files(kind, tmp_path, uploads, runner)

    again = _sync(runner, src)

    assert again["new"] == 0 and again["skipped"] == 2
    assert _rows(runner) == [(1, "a"), (2, "b"), (3, "c")]
    assert _recorded_files(runner) == ["a.csv", "b.csv"]


def test_control_a_failing_file_is_skipped_and_the_others_load(
    kind, tmp_path, uploads, runner, monkeypatch
):
    _write(uploads / "a.csv", ["1,a"])
    _write(uploads / "b.csv", ["2,b"])
    _write(uploads / "c.csv", ["3,c"])
    original = CSVLoader._build_insert_select
    calls = {"n": 0}

    def patched(self, conn, temp_table, target_table):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("bad row in file")
        return original(self, conn, temp_table, target_table)

    monkeypatch.setattr(CSVLoader, "_build_insert_select", patched)

    result = _sync(runner, _source(kind, tmp_path))

    assert result["status"] == "success" and result["new"] == 2
    assert _rows(runner) == [(1, "a"), (3, "c")]
    assert _recorded_files(runner) == ["a.csv", "c.csv"]


def test_control_an_updated_file_still_reloads_in_one_step(kind, tmp_path, uploads, runner):
    src = _seed_two_files(kind, tmp_path, uploads, runner)
    _write(uploads / "a.csv", ["10,x", "20,y", "30,z"])
    os.utime(uploads / "a.csv", (2_000_000_000, 2_000_000_000))

    result = _sync(runner, src)

    assert result["updated"] == 1
    assert _rows(runner) == [(3, "c"), (10, "x"), (20, "y"), (30, "z")]
