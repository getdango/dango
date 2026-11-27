"""tests/unit/test_file_source_dbt_lock.py

WP-66: a csv / local_files sync holds the dbt lock for its whole write window and always releases it (real DuckDB).
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
from dango.exceptions import DbtLockError
from dango.ingestion import dlt_runner, file_refresh
from dango.ingestion.csv_loader import CSVLoader
from dango.ingestion.dlt_runner import DltPipelineRunner, run_sync
from dango.utils.dbt_lock import DbtLock

NAME = "orders"
SCHEMA = f"raw_{NAME}"
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


def _rows(runner: DltPipelineRunner) -> list[tuple]:
    conn = duckdb.connect(str(runner.duckdb_path), config={"access_mode": "read_only"})
    try:
        return conn.execute(f"SELECT id, item FROM {SCHEMA}.{NAME} ORDER BY id").fetchall()
    finally:
        conn.close()


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


def _lock_is_free(root: Path) -> bool:
    """What a dbt run does: try to take the lock without waiting (never leaves it held)."""
    probe = DbtLock(root, source="test", operation="probe")
    try:
        probe.acquire(timeout=0)
    except DbtLockError:
        return False
    probe.release()
    return True


class Probes:
    """Records whether the lock is free each time a patched function runs."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.monkeypatch = monkeypatch
        self.seen: dict[str, list[bool]] = {}

    def __call__(self, owner, attr: str, point: str) -> None:
        original = getattr(owner, attr)

        def wrapper(*args, **kwargs):
            self.seen.setdefault(point, []).append(_lock_is_free(self.root))
            return original(*args, **kwargs)

        self.monkeypatch.setattr(owner, attr, wrapper)

    def assert_busy_at(self, *points: str) -> None:
        for point in points:
            assert self.seen.get(point), f"{point} never ran"
            assert not any(self.seen[point]), f"the lock was free while {point} ran"


@pytest.fixture
def probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Probes:
    return Probes(tmp_path, monkeypatch)


# --- the lock covers the whole write window -------------------------------------------------


def test_lock_is_held_while_a_new_file_loads(kind, tmp_path, uploads, runner, probes):
    _write(uploads / "a.csv", ["1,a"])
    probes(CSVLoader, "_load_new_file", "load_new_file")
    probes(CSVLoader, "load", "loader_load")

    assert _sync(runner, _source(kind, tmp_path))["status"] == "success"

    probes.assert_busy_at("load_new_file", "loader_load")


def test_lock_is_held_while_an_updated_file_reloads(kind, tmp_path, uploads, runner, probes):
    src = _seed(kind, tmp_path, uploads, runner)
    probes(CSVLoader, "_reload_updated_file", "reload")

    assert _sync(runner, src)["status"] == "success"

    probes.assert_busy_at("reload")


def test_lock_covers_full_refresh_preparation_load_and_cleanup(
    kind, tmp_path, uploads, runner, probes, monkeypatch
):
    src = _seed(kind, tmp_path, uploads, runner)
    probes(dlt_runner, "begin_full_refresh", "begin")
    probes(CSVLoader, "load", "loader_load")
    real_connect = dlt_runner._connect_with_lock_retry
    cleanup: list[bool] = []

    def connect(duckdb_path, source_name, operation, project_root=None):
        if operation.endswith("backup-cleanup"):
            cleanup.append(_lock_is_free(tmp_path))
        return real_connect(duckdb_path, source_name, operation, project_root)

    monkeypatch.setattr(dlt_runner, "_connect_with_lock_retry", connect)

    assert _sync(runner, src, full_refresh=True)["status"] == "success"

    probes.assert_busy_at("begin", "loader_load")
    assert cleanup == [False]


def test_lock_is_held_while_an_empty_refresh_is_restored(kind, tmp_path, uploads, runner, probes):
    src = _seed(kind, tmp_path, uploads, runner)
    _write(uploads / "a.csv", [])
    _write(uploads / "b.csv", [])
    probes(dlt_runner, "restore_refresh_backup", "restore")

    assert _sync(runner, src, full_refresh=True)["status"] == "failed"

    probes.assert_busy_at("restore")
    assert _rows(runner) == ORIGINAL


def test_lock_is_held_while_an_aborted_refresh_is_restored(
    kind, tmp_path, uploads, runner, probes, monkeypatch
):
    src = _seed(kind, tmp_path, uploads, runner)

    def interrupted(self, **kwargs):
        raise KeyboardInterrupt

    probes(file_refresh, "restore_refresh_backup", "restore")
    monkeypatch.setattr(CSVLoader, "load", interrupted)

    assert _sync(runner, src, full_refresh=True)["status"] == "interrupted"

    probes.assert_busy_at("restore")
    assert _rows(runner) == ORIGINAL


# --- a busy lock: wait, or fail cleanly without touching the database or Metabase ----------


@pytest.fixture
def metabase_stops(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Fail the test if anything tries to stop Metabase (it would run docker compose)."""
    from dango.platform.common import metabase_lifecycle

    calls: list[bool] = []

    def fake_stop(*args, **kwargs):
        calls.append(True)
        return False

    monkeypatch.setattr(metabase_lifecycle, "stop_metabase_for_writes", fake_stop)
    return calls


def test_busy_lock_fails_the_sync_cleanly_and_changes_nothing(
    kind, tmp_path, uploads, runner, metabase_stops, monkeypatch
):
    src = _seed(kind, tmp_path, uploads, runner)
    connects: list[str] = []
    real_connect = dlt_runner._connect_with_lock_retry
    monkeypatch.setattr(
        dlt_runner,
        "_connect_with_lock_retry",
        lambda *a, **k: connects.append(a[2]) or real_connect(*a, **k),
    )
    holder = DbtLock(tmp_path, source="dbt", operation="dbt run")
    holder.acquire(timeout=0)
    try:
        result = runner.run_source(src, full_refresh=True, max_lock_wait=0)
    finally:
        holder.release()

    assert result["status"] == "failed"
    assert "dbt run" in result["error"] or "another" in result["error"].lower()
    assert "Could not set lock" not in result["error"]
    assert connects == []
    assert metabase_stops == []
    assert _rows(runner) == ORIGINAL
    assert _lock_is_free(tmp_path)


def test_sync_waits_for_a_busy_lock_then_loads(
    kind, tmp_path, uploads, runner, metabase_stops, monkeypatch
):
    _write(uploads / "a.csv", ["1,a"])
    holder = DbtLock(tmp_path, source="dbt", operation="dbt run")
    holder.acquire(timeout=0)
    waits: list[float] = []

    def release_on_first_wait(seconds: float) -> None:
        waits.append(seconds)
        holder.release()

    monkeypatch.setattr("time.sleep", release_on_first_wait)
    try:
        result = runner.run_source(_source(kind, tmp_path), max_lock_wait=30)
    finally:
        holder.release()

    assert waits, "the sync never waited for the busy lock"
    assert result["status"] == "success"
    assert _rows(runner) == [(1, "a")]
    assert metabase_stops == []


def test_interrupt_while_waiting_for_the_lock_is_an_interrupt_that_changed_nothing(
    kind, tmp_path, uploads, runner, metabase_stops, monkeypatch
):
    src = _seed(kind, tmp_path, uploads, runner)
    holder = DbtLock(tmp_path, source="dbt", operation="dbt run")
    holder.acquire(timeout=0)

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", interrupt)
    try:
        result = runner.run_source(src, full_refresh=True, max_lock_wait=30)
    finally:
        holder.release()

    assert result["status"] == "interrupted"
    assert _rows(runner) == ORIGINAL


# --- the lock is always released ------------------------------------------------------------


@pytest.mark.parametrize("raised", ["oserror", "systemexit", "keyboard", "none"])
@pytest.mark.parametrize("full_refresh", [False, True])
def test_lock_is_released_after_success_failure_and_interrupt(
    kind, raised, full_refresh, tmp_path, uploads, runner, monkeypatch
):
    src = _seed(kind, tmp_path, uploads, runner)
    exc = {"oserror": OSError("disk full"), "systemexit": SystemExit(3), "keyboard": None}.get(
        raised
    )
    if raised != "none":

        def fail(self, **kwargs):
            raise exc if exc is not None else KeyboardInterrupt

        monkeypatch.setattr(CSVLoader, "load", fail)

    if raised in ("oserror", "systemexit"):
        with pytest.raises(type(exc)):
            _sync(runner, src, full_refresh=full_refresh)
    else:
        _sync(runner, src, full_refresh=full_refresh)

    assert _lock_is_free(tmp_path)


# --- callers: none of them holds the lock, so none can double-acquire -------------------------


def test_run_sync_takes_the_lock_once_per_file_source_with_the_callers_wait(
    kind, tmp_path, uploads, monkeypatch
):
    """``run_sync`` is what the CLI, the scheduler subprocess, the web initial sync and MCP call."""
    _write(uploads / "a.csv", ["1,a"])
    src = _source(kind, tmp_path)
    acquires: list[tuple[str, float]] = []
    real_acquire = DbtLock.acquire

    def recording(self, timeout: float = 300):
        acquires.append((self.operation, timeout))
        return real_acquire(self, timeout)

    monkeypatch.setattr(DbtLock, "acquire", recording)

    summary = run_sync(tmp_path, [src], skip_dbt=True, max_lock_wait=7)

    assert summary["failed_count"] == 0 and summary["success_count"] == 1
    # (post-sync hooks take the lock for their own writes, after the source's window closed)
    assert [a for a in acquires if a[0].startswith("load:")] == [(f"load:{NAME}", 7)]
    assert _lock_is_free(tmp_path)


def test_run_sync_reports_a_busy_lock_as_a_failed_source(kind, tmp_path, uploads, metabase_stops):
    _write(uploads / "a.csv", ["1,a"])
    holder = DbtLock(tmp_path, source="dbt", operation="dbt run")
    holder.acquire(timeout=0)
    try:
        summary = run_sync(tmp_path, [_source(kind, tmp_path)], skip_dbt=True, max_lock_wait=0)
    finally:
        holder.release()

    assert summary["success_count"] == 0 and summary["failed_count"] == 1
    assert metabase_stops == []
