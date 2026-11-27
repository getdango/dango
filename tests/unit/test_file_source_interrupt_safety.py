"""tests/unit/test_file_source_interrupt_safety.py

WP-64: a Ctrl+C, SIGTERM or failure during a csv / local_files sync (real DuckDB) never loses the previous data.
"""

from __future__ import annotations

import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor
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

NAME = "orders"
SCHEMA = f"raw_{NAME}"
BACKUP = f"{NAME}_pre_refresh_backup"
ORIGINAL = [(1, "a"), (2, "b"), (3, "c")]


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


def _sync_result(runner: DltPipelineRunner, src: DataSource, **kw) -> dict:
    """Like _sync, but a KeyboardInterrupt that escapes (it would abort pytest) is reported."""
    try:
        return _sync(runner, src, **kw)
    except KeyboardInterrupt:
        return {"status": "KeyboardInterrupt escaped"}


def _query(runner: DltPipelineRunner, sql: str) -> list[tuple]:
    conn = duckdb.connect(str(runner.duckdb_path), config={"access_mode": "read_only"})
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _rows(runner: DltPipelineRunner) -> list[tuple]:
    return _query(runner, f"SELECT id, item FROM {SCHEMA}.{NAME} ORDER BY id")


def _metadata(runner: DltPipelineRunner) -> list[tuple]:
    return _query(runner, "SELECT * FROM _dango_file_metadata ORDER BY file_path")


def _tables(runner: DltPipelineRunner) -> set[str]:
    rows = _query(
        runner, "SELECT table_schema, table_name FROM information_schema.tables ORDER BY 1, 2"
    )
    return {f"{s}.{t}" for s, t in rows}


def _seed(kind: str, tmp_path: Path, uploads: Path, runner: DltPipelineRunner) -> DataSource:
    """Two files loaded (3 rows), then the files change on disk so a refresh would differ."""
    _write(uploads / "a.csv", ["1,a", "2,b"])
    _write(uploads / "b.csv", ["3,c"])
    src = _source(kind, tmp_path)
    assert _sync(runner, src)["status"] == "success"
    assert _rows(runner) == ORIGINAL
    _write(uploads / "a.csv", ["10,new-a", "20,new-b"])
    os.utime(uploads / "a.csv", (2_000_000_000, 2_000_000_000))
    return src


def _interrupt_shape(name: str) -> BaseException:
    if name == "duckdb":
        return duckdb.InterruptException("INTERRUPT Error: Interrupted!")
    if name == "wrapped":
        err = RuntimeError("Query interrupted")  # DuckDB's report of a Python signal
        err.__cause__ = KeyboardInterrupt()
        return err
    return KeyboardInterrupt()


def _interrupt_on_call(monkeypatch: pytest.MonkeyPatch, shape: str, call_number: int) -> None:
    """Raise the interrupt inside the per-file DuckDB work of the n-th file."""
    original = CSVLoader._build_insert_select
    calls = {"n": 0}

    def patched(self, conn, temp_table, target_table):
        calls["n"] += 1
        if calls["n"] == call_number:
            raise _interrupt_shape(shape)
        return original(self, conn, temp_table, target_table)

    monkeypatch.setattr(CSVLoader, "_build_insert_select", patched)


def _partial_then_raise(exc: BaseException):
    """A loader that leaves a partial live table and metadata row behind, then raises."""

    def fake(self, **kwargs):
        conn = duckdb.connect(str(self.duckdb_path))
        try:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
            conn.execute(f"CREATE TABLE {SCHEMA}.{NAME} AS SELECT 99 AS id, 'partial' AS item")
            conn.execute(
                "INSERT INTO _dango_file_metadata (source_name, file_path, file_size, "
                "file_mtime, rows_loaded, status, loaded_at) VALUES "
                "('orders', 'partial.csv', 1, now(), 1, 'loaded', now())"
            )
        finally:
            conn.close()
        raise exc

    return fake


SHAPES = ["duckdb", "wrapped", "keyboard"]


@pytest.mark.parametrize("shape", SHAPES)
def test_interrupt_in_plain_sync_is_reported_as_interrupted(
    kind, shape, tmp_path, uploads, runner, monkeypatch
):
    _write(uploads / "a.csv", ["1,a", "2,b"])
    src = _source(kind, tmp_path)
    assert _sync(runner, src)["status"] == "success"
    before_meta = _metadata(runner)
    _write(uploads / "b.csv", ["3,c"])
    _interrupt_on_call(monkeypatch, shape, 1)

    result = _sync_result(runner, src)

    assert result["status"] == "interrupted"
    assert _rows(runner) == [(1, "a"), (2, "b")]
    assert _metadata(runner) == before_meta
    assert not [t for t in _tables(runner) if "_temp_" in t]


@pytest.mark.parametrize("shape", SHAPES)
def test_interrupt_during_full_refresh_restores_previous_data(
    kind, shape, tmp_path, uploads, runner, monkeypatch
):
    src = _seed(kind, tmp_path, uploads, runner)
    before_meta = _metadata(runner)
    _interrupt_on_call(monkeypatch, shape, 2)  # a.csv loaded, b.csv interrupted

    result = _sync_result(runner, src, full_refresh=True)

    assert result["status"] == "interrupted"
    assert _rows(runner) == ORIGINAL
    assert _metadata(runner) == before_meta
    assert f"{SCHEMA}.{BACKUP}" not in _tables(runner)
    assert not [t for t in _tables(runner) if "_temp_" in t]


@pytest.mark.parametrize("raised", ["oserror", "systemexit", "keyboard"])
def test_failed_full_refresh_restores_table_and_metadata(
    kind, raised, tmp_path, uploads, runner, monkeypatch
):
    src = _seed(kind, tmp_path, uploads, runner)
    before_meta = _metadata(runner)
    exc = {
        "oserror": OSError("disk full"),
        "systemexit": SystemExit(3),
        "keyboard": KeyboardInterrupt(),
    }[raised]
    monkeypatch.setattr(CSVLoader, "load", _partial_then_raise(exc))

    if raised == "keyboard":
        assert _sync_result(runner, src, full_refresh=True)["status"] == "interrupted"
    else:
        with pytest.raises(type(exc)):
            _sync(runner, src, full_refresh=True)

    assert _rows(runner) == ORIGINAL
    assert _metadata(runner) == before_meta
    assert f"{SCHEMA}.{BACKUP}" not in _tables(runner)


def test_failed_restore_never_masks_the_original_error(
    kind, tmp_path, uploads, runner, monkeypatch, caplog, capsys
):
    from dango.ingestion import dlt_runner

    src = _seed(kind, tmp_path, uploads, runner)
    monkeypatch.setattr(CSVLoader, "load", _partial_then_raise(OSError("disk full")))
    real_connect = dlt_runner._connect_with_lock_retry
    operations: list[str] = []

    def connect(duckdb_path, source_name, operation, project_root=None):
        operations.append(operation)
        if "restore" in operation:
            raise RuntimeError("secret-detail-xyz")
        return real_connect(duckdb_path, source_name, operation, project_root)

    monkeypatch.setattr(dlt_runner, "_connect_with_lock_retry", connect)

    with caplog.at_level(logging.WARNING), pytest.raises(OSError, match="disk full"):
        _sync(runner, src, full_refresh=True)

    assert any("restore" in op for op in operations)
    shown = caplog.text + capsys.readouterr().out
    assert "RuntimeError" in shown
    assert "secret-detail-xyz" not in shown
    assert f"{SCHEMA}.{BACKUP}" in _tables(runner)  # the previous data is still kept


def test_interrupt_between_rename_and_metadata_snapshot_keeps_the_file_metadata(
    kind, tmp_path, uploads, runner, monkeypatch
):
    """The table is renamed but the metadata rows are not snapshotted (or cleared) yet."""
    from dango.ingestion import file_refresh

    src = _seed(kind, tmp_path, uploads, runner)
    before_meta = _metadata(runner)
    real_print = file_refresh.console.print

    def print_then_interrupt(*args, **kwargs):
        if "existing data backed up" in str(args[0]):
            raise KeyboardInterrupt
        return real_print(*args, **kwargs)

    monkeypatch.setattr(file_refresh.console, "print", print_then_interrupt)

    result = _sync_result(runner, src, full_refresh=True)

    monkeypatch.setattr(file_refresh.console, "print", real_print)
    assert result["status"] == "interrupted"
    assert _rows(runner) == ORIGINAL
    assert _metadata(runner) == before_meta
    assert f"{SCHEMA}.{BACKUP}" not in _tables(runner)
    assert _sync(runner, src)["status"] == "success"  # the next plain sync loads nothing twice
    assert not _query(runner, f"SELECT id FROM {SCHEMA}.{NAME} GROUP BY id HAVING count(*) > 1")


class _InterruptedConn:
    """A connection whose every query is cut short by an interrupt."""

    def __init__(self, shape: str) -> None:
        self.shape = shape

    def execute(self, *args, **kwargs):
        raise _interrupt_shape(self.shape)


@pytest.mark.parametrize("shape", ["duckdb", "wrapped"])
@pytest.mark.parametrize("method", ["has_rows", "file_columns", "delete_file"])
def test_other_per_file_queries_do_not_swallow_an_interrupt(method, shape, tmp_path):
    loader = CSVLoader(tmp_path, tmp_path / "w.duckdb")
    conn = _InterruptedConn(shape)
    call = {
        "has_rows": lambda: loader._has_rows(conn, "a.csv"),
        "file_columns": lambda: loader._get_file_columns(conn, "a.csv"),
        "delete_file": lambda: loader._delete_file_data(conn, "/x/a.csv", "t", NAME),
    }[method]

    with pytest.raises(KeyboardInterrupt):
        call()


# Controls: green before and after the change.


def test_control_normal_full_refresh_replaces_data_and_drops_backup(
    kind, tmp_path, uploads, runner
):
    src = _seed(kind, tmp_path, uploads, runner)

    result = _sync(runner, src, full_refresh=True)

    assert result["status"] == "success"
    assert _rows(runner) == [(3, "c"), (10, "new-a"), (20, "new-b")]
    assert f"{SCHEMA}.{BACKUP}" not in _tables(runner)


def test_control_zero_row_full_refresh_still_restores(kind, tmp_path, uploads, runner):
    src = _seed(kind, tmp_path, uploads, runner)
    before_meta = _metadata(runner)
    _write(uploads / "a.csv", [])
    _write(uploads / "b.csv", [])

    result = _sync(runner, src, full_refresh=True)

    assert result["status"] == "failed"
    assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
    assert _rows(runner) == ORIGINAL
    assert _metadata(runner) == before_meta
    assert f"{SCHEMA}.{BACKUP}" not in _tables(runner)


def test_control_allow_empty_replace_full_refresh_makes_no_backup(kind, tmp_path, uploads, runner):
    src = _seed(kind, tmp_path, uploads, runner)

    result = _sync(runner, src, full_refresh=True, allow_empty_replace=True)

    assert result["status"] == "success"
    assert _rows(runner) == [(3, "c"), (10, "new-a"), (20, "new-b")]
    assert f"{SCHEMA}.{BACKUP}" not in _tables(runner)


def test_control_failing_file_is_still_left_out_not_interrupted(
    kind, tmp_path, uploads, runner, monkeypatch
):
    _write(uploads / "a.csv", ["1,a", "2,b"])
    _write(uploads / "b.csv", ["3,c"])
    original = CSVLoader._build_insert_select
    calls = {"n": 0}

    def patched(self, conn, temp_table, target_table):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("bad row in file")
        return original(self, conn, temp_table, target_table)

    monkeypatch.setattr(CSVLoader, "_build_insert_select", patched)

    result = _sync(runner, _source(kind, tmp_path))

    assert result["status"] == "success"
    assert result["new"] == 1
    assert _rows(runner) == [(1, "a"), (2, "b")]


# SIGTERM inside a file sync becomes the same interrupt as Ctrl+C.


def test_sigterm_becomes_keyboard_interrupt_and_handler_is_restored():
    from dango.ingestion.interrupts import sigterm_as_interrupt

    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt), sigterm_as_interrupt():
        os.kill(os.getpid(), signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) == before


def test_sigterm_conversion_leaves_a_custom_handler_alone():
    from dango.ingestion.interrupts import sigterm_as_interrupt

    def custom(signum, frame):  # pragma: no cover - never called
        raise AssertionError("not called")

    previous = signal.signal(signal.SIGTERM, custom)
    try:
        with sigterm_as_interrupt():
            assert signal.getsignal(signal.SIGTERM) is custom
        assert signal.getsignal(signal.SIGTERM) is custom
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_sigterm_conversion_is_a_no_op_off_the_main_thread():
    from dango.ingestion.interrupts import sigterm_as_interrupt

    def work() -> str:
        with sigterm_as_interrupt():
            return "done"

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(work).result(timeout=10) == "done"
