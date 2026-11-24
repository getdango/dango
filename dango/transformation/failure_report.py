"""dango/transformation/failure_report.py

Turns the output of a failed `run_dbt_models` into what a user is shown once (printable lines) and
what a log records (one short line). Secrets are redacted before anything is cut or escaped.
Never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dango.transformation.build_outcome import (
    ERRORED_NOTE_START,
    NOTICE_PREFIX,
    UNEXPLAINED_NOTE,
    notes_in,
    without_notes,
)
from dango.utils.error_text import redacted_error_text, rich_error_text

REASON_LOG_CHARS = 300
_DETAIL_BLOCKS = 3
_DETAIL_LINES = 3
_STAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\s{2}")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# dbt's per-failure blocks after "Completed with ...": `Failure in model x (path)` (older dbt:
# `Database Error in model x`) and the message lines below it. Test failures are skipped: their names
# are in the notes already.
_HEADER_RE = re.compile(r"^(?:Failure|(?:Database|Runtime|Compilation) Error) in (?!test\b)\w+ ")
_NOISE_RE = re.compile(
    r"^(?:(?:Database|Runtime|Compilation) Error in |compiled code at |LINE \d+:|\^+$)"
)
_CAUSE_RE = re.compile(r"(?:Error in |Error:|timed out|Failure in )")
# dbt prints a compile, parse, profile or dependency error as `Encountered an error:`, then a bare
# `<Kind> Error` (or `<Kind> Error in model x (path)`) line and the message on the next line.
_KIND_HEADER_RE = re.compile(
    r"^(?:Compilation|Parsing|Runtime|Database|Dependency) Error(?: in .*)?$"
)
_FIX_PREFIX = "Suggested fix:"


@dataclass(frozen=True)
class FailureText:
    """What to print for a failed transform. Every line is redacted and Rich-escaped.

    ``reason`` is printed once, after the headline. ``raw`` is dbt's raw output (redacted, cut):
    set only when the verdict gave no usable reason, so a failure never has no cause.
    """

    reason: tuple[str, ...]
    raw: str = ""

    @property
    def explained(self) -> bool:
        """True when ``reason`` says what failed (no raw output is needed)."""
        return not self.raw


def _unstamped(line: str) -> str:
    return _STAMP_RE.sub("", line, count=1)


def _error_details(output: str) -> list[str]:
    """dbt's own error message lines for failed models, seeds, snapshots and hooks."""
    lines = output.splitlines()
    start = next((i for i, ln in enumerate(lines) if "Completed with" in ln), None)
    if start is None:
        return []
    found: list[str] = []
    blocks = 0
    in_block = False
    taken = 0
    for line in lines[start + 1 :]:
        body = _unstamped(line)
        stamped = body != line
        if body.startswith("Done."):
            break
        if _HEADER_RE.match(body):
            in_block = blocks < _DETAIL_BLOCKS
            blocks += 1
            taken = 0
            continue
        if not in_block:
            continue
        text = body.strip()
        if stamped and not text:
            in_block = False
        elif text and not _NOISE_RE.match(text) and taken < _DETAIL_LINES:
            found.append(text)
            taken += 1
    return found


def _is_explained(notes: list[str], details: list[str]) -> bool:
    if not notes or notes == [_sentence(UNEXPLAINED_NOTE)]:
        return False
    errored = any(n.startswith(_sentence(ERRORED_NOTE_START)) for n in notes)
    return bool(details) or not errored  # a failed node whose error text is unknown: show raw


def _sentence(note: str) -> str:
    text = note[len(NOTICE_PREFIX) :].strip() if note.startswith(NOTICE_PREFIX) else note
    return text[:1].upper() + text[1:]


def _suggested_fix(text: str) -> list[str]:
    """The single `Suggested fix:` line of `run_dbt_models`'s structured block, if there is one."""
    for line in text.splitlines():
        if line.startswith(_FIX_PREFIX):
            # the block puts the reason below the fix; here the reason is printed above it
            return [line.strip().replace("named below", "named above")]
    return []


def _first_cause(text: str) -> str:
    """The first line of dbt's output that names an error, with its message line if it has one."""
    lines = [_unstamped(ln).strip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if _KIND_HEADER_RE.match(line):
            message = next((ln for ln in lines[i + 1 :] if ln), "")
            return f"{line}: {message}" if message else line
        if _CAUSE_RE.search(line):
            return line
    return ""


def describe_failure(project_root: Path | None, dbt_output: str) -> FailureText:
    """The printable text of a failed ``run_dbt_models`` output (see ``FailureText``)."""
    redacted = _ANSI_RE.sub("", redacted_error_text(project_root, dbt_output))
    notes = notes_in(redacted)
    details = _error_details(redacted)
    if _is_explained(notes, details):
        shown = [*notes, *details, *_suggested_fix(redacted)]
        return FailureText(reason=tuple(rich_error_text(None, ln) for ln in shown))
    return FailureText(
        reason=tuple(rich_error_text(None, n) for n in notes),
        raw=rich_error_text(None, without_notes(redacted)),
    )


def reason_line(project_root: Path | None, dbt_output: str, limit: int = REASON_LOG_CHARS) -> str:
    """One short plain line saying why a ``run_dbt_models`` build failed (for logs).

    The known reason (notes and dbt's error message); otherwise the first line of dbt's output that
    names an error. Redacted first, then cut to ``limit``; never contains a newline.
    """
    redacted = _ANSI_RE.sub("", redacted_error_text(project_root, dbt_output))
    notes = notes_in(redacted)
    details = _error_details(redacted)
    if _is_explained(notes, details):
        parts = [*notes, *details]
    else:
        parts = [_first_cause(redacted) or (notes[0] if notes else "dbt exited with an error")]
    text = " ".join("; ".join(parts).split())
    return text if len(text) <= limit else f"{text[: limit - 3]}..."
