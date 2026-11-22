"""tests/unit/test_pending_packages_text.py

A failed load says what to do in Dango terms, not in dlt CLI terms; the real cause stays visible.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import dlt
import pytest
from dlt.common.pipeline import LoadInfo
from dlt.destinations.exceptions import DestinationConnectionError
from dlt.pipeline.exceptions import PipelineStepFailed
from rich.console import Console

from dango.cli.sync_report import print_failed_sources
from dango.ingestion.dlt_runner import DltPipelineRunner
from dango.utils.error_text import redacted_error_text, rich_error_text
from dango.utils.redact import redact_error_fields, redact_nested, redact_text
from dango.utils.sync_history import load_sync_history
from tests.unit.test_runner_error_text import _run

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".dlt").mkdir()
    return tmp_path


@pytest.fixture
def out() -> Iterator[StringIO]:
    buf = StringIO()
    with patch("dango.ingestion.dlt_runner.console", Console(file=buf, width=2000, markup=True)):
        yield buf


CAUSE = "IO Error: Cannot open file warehouse.duckdb: Permission denied"
DLT_WORDS = ("dlt pipeline", "drop-pending-packages", "Pending packages are left")
EXPECTED = "This load did not finish"
SECRET_IN_TEXT = "SuperSecretKey99"


def _step_failed(
    *, partially_loaded: bool = False, step: str = "load", cause_text: str = CAUSE
) -> PipelineStepFailed:
    """The exception dlt itself builds for a failed load that left pending packages."""
    pipeline = MagicMock()
    pipeline.pipeline_name = "native"
    pipeline.has_pending_data = True
    info = MagicMock(spec=LoadInfo)
    type(info).__bool__ = lambda self: True
    info.loads_ids = ["L1"]
    package = MagicMock()
    package.load_id = "L1"
    info.load_packages = [package]
    cause = DestinationConnectionError(
        "DuckDbSqlClient", "raw_native", cause_text, RuntimeError("inner")
    )
    with patch(
        "dlt.pipeline.exceptions.PackageStorage.is_package_partially_loaded",
        return_value=partially_loaded,
    ):
        return PipelineStepFailed(pipeline, step, "L1", cause, info)


def _assert_dango_wording(text: str, source: str | None = None) -> None:
    for words in DLT_WORDS:
        assert words not in text
    assert "dlthub.com" not in text
    assert EXPECTED in text
    assert "the cause above was temporary" in text  # a re-run helps only then
    assert "permissions or disk-space problem, fix that first" in text
    assert "--full-refresh" not in text  # not advice that helps for these causes
    assert "<source>" not in text  # no placeholder for the user to edit
    if source:
        assert f"`dango sync {source}`" in text
    else:
        assert "the same command" in text
    assert CAUSE in text  # the real cause stays visible


def test_the_installed_dlt_still_words_the_message_this_rewrite_expects() -> None:
    """Guards the rewrite against a dlt upgrade that rewords the message."""
    text = str(_step_failed())
    assert "Pending packages are left in the pipeline" in text
    assert "drop-pending-packages" in text


def test_redacted_text_uses_dango_wording() -> None:
    _assert_dango_wording(redacted_error_text(None, _step_failed()))


def test_the_real_source_name_is_printed_where_it_is_known() -> None:
    _assert_dango_wording(redacted_error_text(None, _step_failed(), "my_src"), "my_src")
    _assert_dango_wording(rich_error_text(None, _step_failed(), "my_src"), "my_src")


def test_printed_text_uses_dango_wording_and_is_markup_safe() -> None:
    text = rich_error_text(None, _step_failed())
    _assert_dango_wording(text)
    assert "\\[" not in text  # nothing in the new wording needs escaping


def test_partially_loaded_warning_drops_the_dlt_only_advice() -> None:
    text = redacted_error_text(None, _step_failed(partially_loaded=True))
    for words in (*DLT_WORDS, "dlthub.com", "dropping pending packages"):
        assert words not in text
    assert "part of" in text.lower()
    assert "same command again" in text
    assert CAUSE in text


def test_other_errors_are_left_alone() -> None:
    plain = "Server error 503 from https://api.example.com/v1/items (host unavailable)"
    assert redacted_error_text(None, RuntimeError(plain)) == plain
    assert redacted_error_text(None, RuntimeError("Pending items: 3")) == "Pending items: 3"


def test_rewriting_twice_changes_nothing() -> None:
    once = redacted_error_text(None, _step_failed(), "my_src")
    assert redacted_error_text(None, once, "my_src") == once


def test_secrets_are_still_redacted_in_the_same_text() -> None:
    text = redacted_error_text(None, _step_failed(cause_text=f"{CAUSE} api_key={SECRET_IN_TEXT}"))
    assert SECRET_IN_TEXT not in text
    assert EXPECTED in text


class TestLogReadersSeeTheRawText:
    """redact_text feeds MCP get_logs and /api/logs: it must neither rewrite nor log."""

    def test_redact_text_does_not_touch_the_dlt_text(self) -> None:
        raw = str(_step_failed())
        assert redact_text(raw, frozenset()) == raw
        assert redact_nested({"message": raw}, frozenset())["message"] == raw

    def test_no_logging_from_redact_text_however_often_it_is_read(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        raw = str(_step_failed())
        for _ in range(5):
            redact_text(raw, frozenset())
        assert caplog.records == []

    def test_get_logs_serves_the_raw_text_and_does_not_grow_the_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from dango.cli.commands import mcp_debug

        log = tmp_path / ".dango" / "logs" / "dango.log"
        log.parent.mkdir(parents=True)
        line = json.dumps(
            {"level": "debug", "event": "sync_failure_raw", "text": str(_step_failed())}
        )
        log.write_text(line + "\n", encoding="utf-8")
        monkeypatch.setattr(mcp_debug, "_get_project_root", lambda: tmp_path)
        caplog.set_level(logging.DEBUG)
        for _ in range(4):
            result = mcp_debug.get_logs(log="dango", lines=50)
            assert "drop-pending-packages" in json.dumps(result)
        assert [r for r in caplog.records if "advice" in r.getMessage()] == []
        assert log.read_text(encoding="utf-8") == line + "\n"


class TestStoredErrorFields:
    def test_error_fields_are_rewritten_with_the_source_name(self) -> None:
        raw = str(_step_failed())
        record = {"status": "failed", "error_message": raw, "nested_error": [raw], "note": raw}
        fixed = redact_error_fields(None, record, "my_src")
        assert "`dango sync my_src`" in fixed["error_message"]
        assert "`dango sync my_src`" in fixed["nested_error"][0]
        assert fixed["note"] == raw  # only error fields

    def test_history_written_before_the_rewrite_is_rewritten_on_read(self, tmp_path: Path) -> None:
        history = tmp_path / ".dango" / "history" / "native.json"
        history.parent.mkdir(parents=True)
        entry = {"timestamp": "2026-01-01T00:00:00+00:00", "status": "failed"}
        entry["error_message"] = str(_step_failed())
        history.write_text(json.dumps([entry]), encoding="utf-8")
        text = load_sync_history(tmp_path, "native")[0]["error_message"]
        _assert_dango_wording(text, "native")
        assert "drop-pending-packages" in history.read_text(encoding="utf-8")  # file untouched


def test_failed_sync_shows_stores_and_reports_the_dango_wording(
    project: Path,
    out: StringIO,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Terminal output, summary, sync history and activity log agree; raw text logged once."""
    caplog.set_level(logging.DEBUG)
    summary = _run(project, "native", _step_failed(), ("native",))
    printed = out.getvalue().replace("\n", " ")
    for words in DLT_WORDS:
        assert words not in printed
    assert EXPECTED in printed and CAUSE in printed
    assert "`dango sync native`" in printed

    stored = summary["failed_sources"][0]["error"]
    _assert_dango_wording(stored, "native")

    history = load_sync_history(project, "native")[0]["error_message"]
    _assert_dango_wording(history, "native")

    log_file = project / ".dango" / "logs" / "activity.jsonl"
    lines = [json.loads(x)["message"] for x in log_file.read_text(encoding="utf-8").splitlines()]
    failures = [m for m in lines if m.startswith("Sync failed")]
    assert failures
    for message in failures:
        _assert_dango_wording(message, "native")

    raw_logged = [r for r in caplog.records if "drop-pending-packages" in r.getMessage()]
    assert len(raw_logged) == 1  # the raw dlt text goes to the debug log, once per failure


def test_the_raw_debug_line_is_redacted(
    project: Path, out: StringIO, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    exc = _step_failed(cause_text=f"{CAUSE} api_key={SECRET_IN_TEXT}")
    _run(project, "native", exc, ("native",))
    assert "drop-pending-packages" in caplog.text
    assert SECRET_IN_TEXT not in caplog.text
    assert SECRET_IN_TEXT not in out.getvalue()


def test_sync_command_failure_report_names_the_source(
    project: Path,
    out: StringIO,
) -> None:
    summary = _run(project, "native", _step_failed(), ("native",))
    report = StringIO()
    with patch("dango.cli.sync_report.console") as console:
        console.print.side_effect = lambda *a, **k: report.write(" ".join(map(str, a)) + "\n")
        print_failed_sources(summary, project)
    text = report.getvalue().replace("\n", " ")
    assert EXPECTED in text and "`dango sync native`" in text
    assert "drop-pending-packages" not in text


def test_a_read_only_destination_is_not_called_a_network_problem(
    project: Path, out: StringIO
) -> None:
    """dlt words a failed destination open as 'Connection with ... failed'."""
    _run(project, "native", _step_failed(), ("native",))
    printed = out.getvalue().replace("\n", " ")
    assert "Cannot reach API" not in printed
    assert "Network connectivity" not in printed
    assert "could not write to the warehouse" in printed
    assert CAUSE in printed


def test_failure_report_names_the_source_for_text_that_is_still_raw(project: Path) -> None:
    """A summary built from older, unrewritten text gets the real source name when printed."""
    summary = {"failed_sources": [{"name": "native", "error": str(_step_failed())}]}
    report = StringIO()
    with patch("dango.cli.sync_report.console") as console:
        console.print.side_effect = lambda *a, **k: report.write(" ".join(map(str, a)) + "\n")
        print_failed_sources(summary, project)
    assert "`dango sync native`" in report.getvalue().replace("\n", " ")


def test_the_pipeline_failed_line_names_the_source(project: Path, out: StringIO) -> None:
    _run(project, "dlt", _step_failed(), ("native",))
    line = out.getvalue().split("❌ Pipeline failed:")[1].split("Dango could not write")[0]
    assert "`dango sync native`" in line.replace("\n", " ")


STAGED = "nothing was swapped in"


def _failed_staged_refresh(project: Path, exc: Exception) -> dict[str, Any]:
    pipeline = MagicMock(spec=dlt.Pipeline)
    pipeline.extract.return_value = None
    pipeline.normalize.side_effect = exc
    runner = DltPipelineRunner(project)
    with (
        patch("dlt.pipeline", return_value=pipeline),
        patch("dango.ingestion.dlt_runner._connect_with_lock_retry"),
        patch.object(DltPipelineRunner, "_clear_local_pipeline_cache"),
    ):
        return runner._full_refresh_via_staging(
            source=MagicMock(),
            source_name="native",
            dataset_name="raw_native",
            pipeline_name="native",
            max_lock_wait=1,
            uses_replace_mode=False,
        )


class TestFailedStagedFullRefresh:
    """The staging pipeline is cleared after a failure: its data is not kept for a next run."""

    def test_the_advice_says_what_happened_not_that_the_data_is_kept(
        self, project: Path, out: StringIO
    ) -> None:
        result = _failed_staged_refresh(project, _step_failed())
        assert result["status"] == "failed"
        for text in (result["error"], out.getvalue()):
            flat = " ".join(text.split())
            assert "Its data is kept" not in flat and "next run loads it" not in flat
            assert STAGED in flat and "existing tables are unchanged" in flat
            assert "staged data was discarded" in flat
            assert "run the same command again" in flat
            assert CAUSE in flat  # the real cause stays visible
            for words in DLT_WORDS:
                assert words not in flat

    def test_a_partly_loaded_staging_package_is_described_the_same_way(
        self, project: Path, out: StringIO
    ) -> None:
        result = _failed_staged_refresh(project, _step_failed(partially_loaded=True))
        flat = " ".join(result["error"].split())
        assert "Part of the previous load was written to the warehouse" not in flat
        assert "existing tables are unchanged" in flat and "dlthub.com" not in flat

    def test_an_ordinary_load_failure_keeps_its_own_wording(self) -> None:
        text = redacted_error_text(None, _step_failed(), "native")
        assert "Its data is kept and the next run loads it" in text
        assert STAGED not in text


def test_a_known_secret_on_the_rewritten_line_is_masked_before_the_rewrite(
    tmp_path: Path,
) -> None:
    """Redaction runs first. A project secret that overlaps the start of dlt's sentence is masked
    in the original text, so the sentence is left redacted, not replaced; swapping the two steps
    would let the rewrite decide what is a secret."""
    secret = "Pending packages are left"
    (tmp_path / ".env").write_text(f"SOME_TOKEN={secret}\n", encoding="utf-8")
    for text in (
        redacted_error_text(tmp_path, _step_failed(), "native"),
        rich_error_text(tmp_path, _step_failed(), "native"),
    ):
        assert secret not in text
        assert "[REDACTED] in the pipeline" in text
        assert EXPECTED not in text  # the masked line no longer matches the rewrite
