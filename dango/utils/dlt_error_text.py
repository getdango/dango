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


def pending_advice(source_name: str | None = None) -> str:
    """The replacement for dlt's pending-package advice (names the source when it is known)."""
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


def rewrite_dlt_advice(text: str, source_name: str | None = None) -> str:
    """*text* with dlt's pending-package and partial-load advice replaced by Dango's wording."""
    if "Pending packages" not in text and "partially loaded" not in text:
        return text
    advice = pending_advice(source_name)
    text = _PENDING.sub(lambda _match: advice, text)
    return _PARTIAL.sub(lambda _match: PARTIAL_TEXT, text)
