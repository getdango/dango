"""dango/utils/dlt_error_text.py

Say what to do after a failed load in Dango terms instead of dlt command-line terms.

Level 0 module (standard library only). dlt ends the error of a load that failed after extract with
advice to run `dlt pipeline <name> info` or `drop-pending-packages`. Dango users do not run the dlt
command line, so that advice is replaced; the real cause in the text before it is kept. The raw
passage goes to the debug log (the caller redacts the text first).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# dlt's text is one line each (see dlt.pipeline.exceptions.PipelineStepFailed); a test checks the
# installed dlt still words it this way.
_PENDING = re.compile(r"Pending packages are left in the pipeline[^\n]*")
_PARTIAL = re.compile(r"WARNING: package `[^`\n]*` is partially loaded[^\n]*")

PENDING_TEXT = (
    "A previous load did not finish. Run the same command again; if it keeps failing, run "
    "`dango sync <source> --full-refresh`."
)
PARTIAL_TEXT = (
    "Part of the previous load was written to the warehouse, so it may hold only part of that "
    "load's data. Run the same command again to finish it."
)


def _replace_all(pattern: re.Pattern[str], replacement: str, text: str) -> str:
    """Replace every match with *replacement* literally (no backslash or group handling)."""
    return pattern.sub(lambda _match: replacement, text)


def rewrite_dlt_advice(text: str) -> str:
    """*text* with dlt's pending-package and partial-load advice replaced by Dango's wording."""
    if "Pending packages" not in text and "partially loaded" not in text:
        return text
    for pattern, replacement in ((_PENDING, PENDING_TEXT), (_PARTIAL, PARTIAL_TEXT)):
        for match in pattern.finditer(text):
            logger.debug("dlt_load_advice_raw: %s", match.group(0))
        text = _replace_all(pattern, replacement, text)
    return text
