"""tests/unit/test_failure_report.py

`describe_failure` and `reason_line` turn the output of a failed `run_dbt_models` into the lines a
user is shown once and the one line a log records: a known reason is shown without dbt's raw dump,
an unknown one falls back to the raw output, and secrets and Rich markup are handled.
"""

from __future__ import annotations

import pytest

from dango.transformation.build_outcome import UNEXPLAINED_NOTE
from dango.transformation.failure_report import describe_failure, reason_line

pytestmark = pytest.mark.unit

SECRET = "hunter2hunter2"
STRUCTURED = "dbt build failed\n\nPossible causes:\n  - something\n\nSuggested fix: fix it"
SKIPPED_NOTES = (
    "dango: models skipped because something they depend on failed (2): stg_a, mart_b\n"
    "dango: tests failed (1): unique_stg_a_id\n"
)
MODEL_ERROR_RAW = (
    "05:23:54  Completed with 1 error, 0 partial successes, and 0 warnings:\n"
    "05:23:54  \n"
    "05:23:54  Failure in model broken_mart (models/marts/broken_mart.sql)\n"
    "05:23:54    Runtime Error in model broken_mart (models/marts/broken_mart.sql)\n"
    "  Conversion Error: Could not convert string 'abc' to INT32\n"
    "  \n"
    "  LINE 10:       select cast('abc' as integer) as x from \"warehouse\"...\n"
    "                        ^\n"
    "05:23:54  \n"
    "05:23:54    compiled code at target/compiled/proj/models/marts/broken_mart.sql\n"
    "05:23:54  \n"
    "05:23:54  Done. PASS=4 WARN=0 ERROR=1 SKIP=0 NO-OP=0 TOTAL=5\n"
)
ERRORED_NOTE = "dango: failed models, seeds, snapshots or hooks (1): broken_mart\n"


def _output(notes: str, raw: str) -> str:
    return f"{STRUCTURED}\n\n{notes}\nFull output:\n{raw}"


def test_known_reason_prints_the_notes_and_no_raw_output() -> None:
    raw = "05:12:16  Running with dbt=1.11.14\n05:12:17  FAIL 1 unique_stg_a_id\n"
    failure = describe_failure(None, _output(SKIPPED_NOTES, raw))
    assert failure.explained
    assert failure.raw == ""
    assert failure.reason == (
        "Models skipped because something they depend on failed (2): stg_a, mart_b",
        "Tests failed (1): unique_stg_a_id",
    )


def test_errored_model_adds_dbts_error_message_once() -> None:
    failure = describe_failure(None, _output(ERRORED_NOTE, MODEL_ERROR_RAW))
    assert failure.explained
    assert failure.reason == (
        "Failed models, seeds, snapshots or hooks (1): broken_mart",
        "Conversion Error: Could not convert string 'abc' to INT32",
    )


def test_ansi_colors_in_dbt_output_are_removed() -> None:
    colored = MODEL_ERROR_RAW.replace("Failure in model", "\x1b[31mFailure in model\x1b[0m")
    failure = describe_failure(None, _output(ERRORED_NOTE, colored))
    assert failure.explained
    assert not any("\x1b" in line for line in failure.reason)
    assert "\x1b" not in reason_line(None, _output(ERRORED_NOTE, colored))


def test_errored_node_without_a_readable_error_falls_back_to_the_raw_output() -> None:
    failure = describe_failure(None, _output(ERRORED_NOTE, "05:12:17  something odd\n"))
    assert not failure.explained
    assert "something odd" in failure.raw
    assert failure.reason == ("Failed models, seeds, snapshots or hooks (1): broken_mart",)


def test_unexplained_failure_shows_the_raw_output_once() -> None:
    raw = "05:12:16  Compilation Error in model bad_ref\n05:12:16    depends on a node named x\n"
    failure = describe_failure(None, _output(UNEXPLAINED_NOTE + "\n", raw))
    assert not failure.explained
    assert failure.raw.count("Compilation Error in model bad_ref") == 1
    assert "dango: " not in failure.raw  # the note is a reason line, not part of the raw text
    assert len(failure.reason) == 1


def test_output_without_any_note_is_shown_raw() -> None:
    failure = describe_failure(None, "dbt build failed: [Errno 2] No such file")
    assert not failure.explained
    assert "No such file" in failure.raw


def test_rich_markup_is_escaped_in_reason_and_raw() -> None:
    notes = "dango: tests failed (1): [bold]x[/bold]\n"
    reason = describe_failure(None, _output(notes, "raw"))
    assert reason.reason == ("Tests failed (1): \\[bold]x\\[/bold]",)
    raw = describe_failure(None, "odd [red]line[/red]")
    assert "\\[red]line\\[/red]" in raw.raw


def test_secrets_are_redacted_in_reason_raw_and_log_line() -> None:
    notes = f"dango: tests failed (1): password={SECRET}\n"
    known = _output(notes, "raw")
    unknown = f"05:12:17  Database Error: password={SECRET} rejected\n"
    for text in (known, unknown):
        failure = describe_failure(None, text)
        assert SECRET not in " ".join(failure.reason) + failure.raw
        assert SECRET not in reason_line(None, text)


def test_reason_line_is_redacted_before_it_is_cut() -> None:
    text = f"05:12:17  Database Error: {'x' * 280} password={SECRET}\n"
    line = reason_line(None, text, limit=300)
    assert SECRET not in line
    assert len(line) <= 300


def test_reason_line_is_one_short_line() -> None:
    names = ", ".join(f"m{i}" for i in range(500))
    line = reason_line(None, f"dango: failed models, seeds, snapshots or hooks (500): {names}\n")
    assert "\n" not in line
    assert len(line) <= 300
    assert line.endswith("...")


def test_reason_line_for_a_known_reason_joins_the_notes() -> None:
    line = reason_line(None, _output(SKIPPED_NOTES, "raw"))
    assert line == (
        "Models skipped because something they depend on failed (2): stg_a, mart_b; "
        "Tests failed (1): unique_stg_a_id"
    )


def test_reason_line_without_a_reason_names_dbts_first_error_line() -> None:
    raw = "05:12:16  Running with dbt\n05:12:16  Compilation Error in model bad_ref (x.sql)\n"
    assert reason_line(None, _output(UNEXPLAINED_NOTE + "\n", raw)) == (
        "Compilation Error in model bad_ref (x.sql)"
    )


def test_reason_line_never_comes_back_empty() -> None:
    assert reason_line(None, "") == "dbt exited with an error"
