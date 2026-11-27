"""tests/unit/test_file_source_failed_file_reasons.py

WP-67: what a failed file's reason and the failed-files error may contain (names, no paths, secrets or cell values).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from dango.config.models import DataSource, LocalFilesSourceConfig, SourceType
from dango.ingestion.csv_loader import CSVLoader
from dango.ingestion.dlt_runner import DltPipelineRunner
from tests.factories.partial_file_source import (
    NAME,
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


def _loader(tmp_path: Path) -> CSVLoader:
    return CSVLoader(tmp_path, tmp_path / "w.duckdb")


def test_the_second_sync_after_a_bad_file_names_it(kind, tmp_path, uploads, runner) -> None:
    write_good(uploads / "a.csv", ["1,a"])
    src = make_source(kind, uploads)
    assert run_method(runner, src)["status"] == "success"
    write_good(uploads / "b.csv", ["2,b"])
    write_latin1(uploads / "c.csv")
    assert run_method(runner, src)["status"] == "partial"

    again = run_method(runner, src)  # nothing new loads: the bad file is the only one attempted

    assert again["status"] == "error"
    assert again["error"].startswith("No data loaded from 1 file(s)")
    assert "c.csv" in again["error"]
    assert [f["file"] for f in again["failed_files"]] == ["c.csv"]


def test_a_conversion_error_does_not_carry_the_cell_value(kind, tmp_path, uploads, runner) -> None:
    write_good(uploads / "a.csv", ["1,5"])
    src = make_source(kind, uploads)
    assert run_method(runner, src)["status"] == "success"
    rows = ["id,item"] + [f"{i},{i}" for i in range(30000)] + ["30001,SECRETCELL"]
    (uploads / "b.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    write_good(uploads / "d.csv", ["4,7"])

    result = run_method(runner, src)

    assert result["status"] == "partial"
    reason = result["failed_files"][0]["reason"]
    assert "Could not convert string" in reason
    assert "SECRETCELL" not in reason and "SECRETCELL" not in result["error"]


def test_a_malformed_jsonl_file_is_named_without_its_path(tmp_path, uploads, runner) -> None:
    (uploads / "a.jsonl").write_text('{"id":1}\n', encoding="utf-8")
    config = LocalFilesSourceConfig(directory=uploads, file_pattern="*.jsonl")
    src = DataSource(name=NAME, type=SourceType.LOCAL_FILES, local_files=config)
    assert runner._run_local_files_source(src)["status"] == "success"
    (uploads / "b.jsonl").write_text('{"id":2}\n', encoding="utf-8")
    lines = "".join(f'{{"id":{i}}}\n' for i in range(30000))
    (uploads / "c.jsonl").write_text(lines + '{"id": \n', encoding="utf-8")

    result = runner._run_local_files_source(src)

    assert result["status"] == "partial"
    assert "c.jsonl" in result["error"]
    assert str(tmp_path) not in result["error"]
    assert str(tmp_path) not in result["failed_files"][0]["reason"]


def test_a_secret_in_the_failure_text_is_masked(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("K=FailureSecret12345\n", encoding="utf-8")
    loader = _loader(tmp_path)

    loader._record_failed_file(str(tmp_path / "b.csv"), RuntimeError("bad FailureSecret12345 x"))

    assert "FailureSecret12345" not in loader.failed_files[0]["reason"]


def test_a_long_reason_is_cut(tmp_path: Path) -> None:
    loader = _loader(tmp_path)

    loader._record_failed_file(str(tmp_path / "b.csv"), RuntimeError("x" * 500))

    reason = loader.failed_files[0]["reason"]
    assert reason.endswith("...") and len(reason) <= 203


def test_at_most_five_files_are_named(tmp_path: Path) -> None:
    failed = [{"file": f"f{i}.csv", "reason": "r"} for i in range(7)]

    text = CSVLoader._failed_files_error(failed)

    assert text.startswith("7 file(s) failed")
    assert "f4.csv" in text and "f5.csv" not in text
    assert "; and 2 more" in text


def test_an_interrupt_during_a_reload_is_not_a_failed_file(
    kind, tmp_path, uploads, runner, monkeypatch
) -> None:
    write_good(uploads / "a.csv", ["1,a"])
    write_good(uploads / "b.csv", ["2,b"])
    src = make_source(kind, uploads)
    assert run_method(runner, src)["status"] == "success"
    write_good(uploads / "b.csv", ["2,changed"])
    bump_mtime(uploads / "b.csv")

    def interrupted(self, conn, temp_table, target_table):
        raise duckdb.InterruptException("interrupted")

    with patch.object(CSVLoader, "_build_insert_select", interrupted):
        result = run_method(runner, src)

    assert result["status"] == "interrupted"
    assert "failed_files" not in result
