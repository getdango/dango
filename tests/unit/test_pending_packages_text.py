"""tests/unit/test_pending_packages_text.py

A failed load says what to do in Dango terms, not in dlt CLI terms; the real cause stays visible.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dlt.common.pipeline import LoadInfo
from dlt.destinations.exceptions import DestinationConnectionError
from dlt.pipeline.exceptions import PipelineStepFailed
from rich.console import Console

from dango.cli.sync_report import print_failed_sources
from dango.utils.error_text import redacted_error_text, rich_error_text
from dango.utils.redact import redact_nested, redact_text
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
EXPECTED = "A previous load did not finish. Run the same command again"


def _step_failed(*, partially_loaded: bool = False, step: str = "load") -> PipelineStepFailed:
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
        "DuckDbSqlClient", "raw_native", CAUSE, RuntimeError("inner")
    )
    with patch(
        "dlt.pipeline.exceptions.PackageStorage.is_package_partially_loaded",
        return_value=partially_loaded,
    ):
        return PipelineStepFailed(pipeline, step, "L1", cause, info)


def _assert_dango_wording(text: str) -> None:
    for words in DLT_WORDS:
        assert words not in text
    assert "dlthub.com" not in text
    assert EXPECTED in text
    assert "dango sync <source> --full-refresh" in text
    assert CAUSE in text  # the real cause stays visible


def test_the_installed_dlt_still_words_the_message_this_rewrite_expects() -> None:
    """Guards the rewrite against a dlt upgrade that rewords the message."""
    text = str(_step_failed())
    assert "Pending packages are left in the pipeline" in text
    assert "drop-pending-packages" in text


def test_redacted_text_uses_dango_wording() -> None:
    _assert_dango_wording(redacted_error_text(None, _step_failed()))


def test_printed_text_uses_dango_wording_and_is_markup_safe() -> None:
    text = rich_error_text(None, _step_failed())
    _assert_dango_wording(text)
    assert "\\[" not in text  # nothing in the new wording needs escaping


def test_redact_text_is_the_shared_sink() -> None:
    """History, activity log, MCP, scheduler and web text all pass through redact_text."""
    raw = str(_step_failed())
    assert EXPECTED in redact_text(raw, frozenset())
    assert EXPECTED in redact_nested({"error": raw, "rows": [raw]}, frozenset())["rows"][0]


def test_partially_loaded_warning_drops_the_dlt_only_advice() -> None:
    text = redacted_error_text(None, _step_failed(partially_loaded=True))
    for words in (*DLT_WORDS, "dlthub.com", "dropping pending packages"):
        assert words not in text
    assert "partly" in text.lower() or "part of" in text.lower()
    assert "same command again" in text
    assert CAUSE in text


def test_other_errors_are_left_alone() -> None:
    plain = "Server error 503 from https://api.example.com/v1/items (host unavailable)"
    assert redacted_error_text(None, RuntimeError(plain)) == plain
    assert redacted_error_text(None, RuntimeError("Pending items: 3")) == "Pending items: 3"


def test_rewriting_twice_changes_nothing() -> None:
    once = redacted_error_text(None, _step_failed())
    assert redact_text(once, frozenset()) == once


def test_secrets_are_still_redacted_in_the_same_text() -> None:
    raw = str(_step_failed()) + "\nurl=https://u:Sup3rSecretPw@host/db"
    text = redact_text(raw, frozenset())
    assert "Sup3rSecretPw" not in text
    assert EXPECTED in text


def test_raw_dlt_text_goes_to_the_debug_log_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    text = redacted_error_text(None, _step_failed())
    assert "drop-pending-packages" not in text
    assert "drop-pending-packages" in caplog.text


def test_failed_sync_shows_stores_and_reports_the_dango_wording(
    project: Path,
    out: StringIO,
) -> None:
    """Terminal output, summary, sync history, activity log and the summary dict agree."""
    summary = _run(project, "native", _step_failed(), ("native",))
    printed = out.getvalue()
    for words in DLT_WORDS:
        assert words not in printed
    assert EXPECTED in printed.replace("\n", " ")
    assert CAUSE in printed.replace("\n", " ")

    stored = summary["failed_sources"][0]["error"]
    assert EXPECTED in stored and CAUSE in stored
    assert all(words not in stored for words in DLT_WORDS)

    history = load_sync_history(project, "native")[0]["error_message"]
    assert EXPECTED in history and all(words not in history for words in DLT_WORDS)

    log_file = project / ".dango" / "logs" / "activity.jsonl"
    lines = [json.loads(x)["message"] for x in log_file.read_text(encoding="utf-8").splitlines()]
    failures = [m for m in lines if m.startswith("Sync failed")]
    assert failures and all(EXPECTED in m for m in failures)
    assert all(words not in m for m in failures for words in DLT_WORDS)


def test_sync_command_failure_report_uses_the_dango_wording(
    project: Path,
    out: StringIO,
) -> None:
    summary = _run(project, "native", _step_failed(), ("native",))
    report = StringIO()
    with patch("dango.cli.sync_report.console") as console:
        console.print.side_effect = lambda *a, **k: report.write(" ".join(map(str, a)) + "\n")
        print_failed_sources(summary, project)
    assert EXPECTED in report.getvalue().replace("\n", " ")
    assert "drop-pending-packages" not in report.getvalue()
