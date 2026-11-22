"""dango/utils/dlt_error_text.py

Say what to do after a failed load in Dango terms instead of dlt command-line terms.

Level 0 module (standard library only). dlt ends the error of a load that failed after extract with
advice to run `dlt pipeline <name> info` or `drop-pending-packages`. Dango users do not run the dlt
command line, so that advice is replaced; the real cause in the text before it is kept. This is
applied where Dango builds or reads back a failure's error text (never inside ``redact_text``,
which log readers use and which must show a log line as it was written). It does not log: the
caller logs the raw text once, redacted, where the failure is recorded.
"""

from __future__ import annotations

import re

# dlt's text is one line each (see dlt.pipeline.exceptions.PipelineStepFailed); a test checks the
# installed dlt still words it this way.
_PENDING = re.compile(r"Pending packages are left in the pipeline[^\n]*")
_PARTIAL = re.compile(r"WARNING: package `[^`\n]*` is partially loaded[^\n]*")


def pending_advice(source_name: str | None = None, *, kept: bool = True) -> str:
    """The replacement for dlt's pending-package advice (names the source when it is known).

    ``kept=False`` is for a failed staged full refresh: Dango clears the staging pipeline after
    the failure, so the staged data is not kept for a next run; nothing was swapped in.
    """
    if not kept:
        return STAGED_PENDING_TEXT
    command = f"`dango sync {source_name}`" if source_name else "the same command"
    return (
        "This load did not finish. Its data is kept and the next run loads it, so running "
        f"{command} again helps if the cause above was temporary. If it was a permissions or "
        "disk-space problem, fix that first."
    )


PARTIAL_TEXT = (
    "Part of the previous load was written to the warehouse, so it may hold only part of that "
    "load's data. Run the same command again to finish it."
)

# A failed staged full refresh: the swap never happened and the staging pipeline was cleared.
STAGED_PENDING_TEXT = (
    "This full refresh did not finish: nothing was swapped in, the existing tables are unchanged "
    "and the staged data was discarded. Fix the cause above (a permissions or disk-space problem, "
    "for example), then run the same command again."
)
STAGED_PARTIAL_TEXT = (
    "The staged data was only partly loaded and was discarded: nothing was swapped in and the "
    "existing tables are unchanged. Fix the cause above, then run the same command again."
)


def rewrite_dlt_advice(text: str, source_name: str | None = None, *, kept: bool = True) -> str:
    """*text* with dlt's pending-package and partial-load advice replaced by Dango's wording.

    ``kept=False`` is for the error of a failed staged full refresh (see ``pending_advice``).
    """
    if "Pending packages" not in text and "partially loaded" not in text:
        return text
    advice = pending_advice(source_name, kept=kept)
    partial = PARTIAL_TEXT if kept else STAGED_PARTIAL_TEXT
    text = _PENDING.sub(lambda _match: advice, text)
    return _PARTIAL.sub(lambda _match: partial, text)
