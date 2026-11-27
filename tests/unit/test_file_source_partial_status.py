"""tests/unit/test_file_source_partial_status.py

WP-67: a csv / local_files sync where a file failed to load is "partial", not "success" (real DuckDB).
"""

from __future__ import annotations

from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
from rich.console import Console

from dango.ingestion.dlt_runner import DltPipelineRunner, run_sync
from dango.utils.sync_history import load_sync_history
from tests.factories.partial_file_source import (
    NAME,
    SCHEMA,
    bump_mtime,
    make_source,
    run_method,
    write_good,
    write_latin1,
)

pytestmark = pytest.mark.unit


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


@pytest.fixture
def out() -> Iterator[StringIO]:
    buf = StringIO()
    with patch("dango.ingestion.dlt_runner.console", Console(file=buf, width=2000)):
        yield buf


def _query(runner: DltPipelineRunner, sql: str) -> list[tuple]:
    conn = duckdb.connect(str(runner.duckdb_path), config={"access_mode": "read_only"})
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _ids(runner: DltPipelineRunner) -> list[int]:
    return [r[0] for r in _query(runner, f"SELECT id FROM {SCHEMA}.{NAME} ORDER BY id")]


def _recorded(runner: DltPipelineRunner) -> list[str]:
    rows = _query(runner, "SELECT file_path FROM _dango_file_metadata ORDER BY file_path")
    return [Path(r[0]).name for r in rows]


def _seed(kind: str, tmp_path: Path, uploads: Path, runner: DltPipelineRunner):
    write_good(uploads / "a.csv", ["1,a"])
    src = make_source(kind, uploads)
    assert run_method(runner, src)["status"] == "success"
    return src


def test_new_bad_file_among_good_files_is_partial(kind, tmp_path, uploads, runner) -> None:
    src = _seed(kind, tmp_path, uploads, runner)
    write_good(uploads / "b.csv", ["2,b"])
    write_latin1(uploads / "c.csv")

    result = run_method(runner, src)

    assert result["status"] == "partial"
    assert [f["file"] for f in result["failed_files"]] == ["c.csv"]
    assert "utf-8" in result["failed_files"][0]["reason"].lower()
    assert "c.csv" in result["error"]
    assert "\n" not in result["error"]
    assert str(tmp_path) not in result["error"]
    assert result["rows_loaded"] == 2
    assert _ids(runner) == [1, 2]
    assert _recorded(runner) == ["a.csv", "b.csv"]


def test_updated_file_that_fails_to_reload_is_partial(kind, tmp_path, uploads, runner) -> None:
    src = _seed(kind, tmp_path, uploads, runner)
    write_good(uploads / "b.csv", ["2,b"])
    assert run_method(runner, src)["status"] == "success"
    write_latin1(uploads / "b.csv")
    bump_mtime(uploads / "b.csv")
    write_good(uploads / "d.csv", ["4,d"])

    result = run_method(runner, src)

    assert result["status"] == "partial"
    assert [f["file"] for f in result["failed_files"]] == ["b.csv"]
    assert _ids(runner) == [1, 2, 4]  # the old version of b.csv is kept


def test_the_failed_file_is_reported_again_until_it_is_fixed(
    kind, tmp_path, uploads, runner
) -> None:
    src = _seed(kind, tmp_path, uploads, runner)
    write_latin1(uploads / "c.csv")
    write_good(uploads / "b.csv", ["2,b"])
    assert run_method(runner, src)["status"] == "partial"

    again = run_method(runner, src)
    assert again["status"] == "error"  # nothing new loaded this time: every attempt failed
    assert "No data loaded" in again["error"]

    write_good(uploads / "c.csv", ["3,c"])
    bump_mtime(uploads / "c.csv")
    fixed = run_method(runner, src)
    assert fixed["status"] == "success"
    assert "failed_files" not in fixed
    assert _ids(runner) == [1, 2, 3]  # loaded exactly once
    again = run_method(runner, src)
    assert again["status"] == "success" and again["new"] == 0
    assert _ids(runner) == [1, 2, 3]


def test_a_still_bad_file_with_a_new_good_file_is_partial_again(
    kind, tmp_path, uploads, runner
) -> None:
    src = _seed(kind, tmp_path, uploads, runner)
    write_latin1(uploads / "c.csv")
    write_good(uploads / "b.csv", ["2,b"])
    assert run_method(runner, src)["status"] == "partial"
    write_good(uploads / "d.csv", ["4,d"])

    result = run_method(runner, src)

    assert result["status"] == "partial"
    assert [f["file"] for f in result["failed_files"]] == ["c.csv"]
    assert _ids(runner) == [1, 2, 4]


def test_full_refresh_with_a_failing_file_keeps_the_files_that_loaded(
    kind, tmp_path, uploads, runner
) -> None:
    src = _seed(kind, tmp_path, uploads, runner)
    write_good(uploads / "b.csv", ["2,b"])
    assert run_method(runner, src)["status"] == "success"
    write_latin1(uploads / "c.csv")

    result = run_method(runner, src, full_refresh=True)

    assert result["status"] == "partial"
    assert [f["file"] for f in result["failed_files"]] == ["c.csv"]
    assert _ids(runner) == [1, 2]
    tables = _query(
        runner,
        f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{SCHEMA}'",
    )
    assert [t[0] for t in tables] == [NAME]  # no leftover backup table


def test_run_source_records_partial_and_does_not_print_synced(
    kind, tmp_path, uploads, runner, out
) -> None:
    src = _seed(kind, tmp_path, uploads, runner)
    write_good(uploads / "b.csv", ["2,b"])
    write_latin1(uploads / "c.csv")
    out.truncate(0)

    result = runner.run_source(src)

    assert result["status"] == "partial"
    last = load_sync_history(tmp_path, NAME, limit=1)[0]
    assert last["status"] == "partial"
    assert "c.csv" in last["error_message"]
    printed = out.getvalue()
    assert "Synced" not in printed
    assert "c.csv" in printed


def test_run_sync_lists_the_source_under_failures_with_the_file(
    kind, tmp_path, uploads, runner, out
) -> None:
    src = _seed(kind, tmp_path, uploads, runner)
    write_good(uploads / "b.csv", ["2,b"])
    write_latin1(uploads / "c.csv")
    out.truncate(0)

    summary = run_sync(tmp_path, [src], skip_dbt=True)

    assert summary["success_count"] == 0
    assert summary["failed_count"] == 1
    failed = summary["failed_sources"][0]
    assert failed["name"] == NAME and "c.csv" in failed["error"]
    assert summary["partial_sources"] == [NAME]
    printed = out.getvalue()
    assert "Failed (1)" in printed and "c.csv" in printed
    assert "Succeeded" not in printed


# ---- controls: these hold on the unmodified code too ----


def test_every_file_good_is_success(kind, tmp_path, uploads, runner) -> None:
    src = _seed(kind, tmp_path, uploads, runner)
    write_good(uploads / "b.csv", ["2,b"])
    result = run_method(runner, src)
    assert result["status"] == "success" and "failed_files" not in result
    summary = run_sync(tmp_path, [src], skip_dbt=True)
    assert summary["failed_count"] == 0 and summary["success_count"] == 1


def test_every_attempted_file_failing_is_an_error_as_before(
    kind, tmp_path, uploads, runner
) -> None:
    src = _seed(kind, tmp_path, uploads, runner)
    write_latin1(uploads / "c.csv")
    result = run_method(runner, src)
    assert result["status"] == "error"
    assert "No data loaded from 1 file(s)" in result["error"]
    assert _ids(runner) == [1]


def test_a_zero_row_file_is_not_a_failure(kind, tmp_path, uploads, runner) -> None:
    src = _seed(kind, tmp_path, uploads, runner)
    write_good(uploads / "b.csv", ["2,b"])
    write_good(uploads / "empty.csv", [])
    result = run_method(runner, src)
    assert result["status"] == "success" and "failed_files" not in result
    assert _ids(runner) == [1, 2]


def test_a_schema_mismatch_still_fails_fast(kind, tmp_path, uploads, runner) -> None:
    src = _seed(kind, tmp_path, uploads, runner)
    write_good(uploads / "b.csv", ["2,b"])
    (uploads / "c.csv").write_text("other,cols\n1,2\n", encoding="utf-8")
    result = run_method(runner, src)
    assert result["status"] == "error"
    assert "Schema validation failed" in result["error"]
    assert "failed_files" not in result
    assert _ids(runner) == [1]  # nothing from this run was loaded
