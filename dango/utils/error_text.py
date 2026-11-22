"""dango/utils/error_text.py

One place that turns error text into something safe to print: secrets redacted, Rich markup escaped.
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape

from dango.utils.dlt_error_text import rewrite_dlt_advice
from dango.utils.duckdb_lock_text import rewrite_lock_text
from dango.utils.redact import redact_project_text

# Longest error text printed to a terminal (stored records keep the full text).
MAX_PRINTED_CHARS = 8000


def redacted_error_text(
    project_root: Path | None,
    error: object,
    source_name: str | None = None,
    *,
    kept: bool = True,
) -> str:
    """Plain text of *error* with secrets masked (for stored text and printed errors).

    dlt's command-line advice after a failed load is replaced by Dango's wording, naming
    *source_name* when it is known (``kept=False``: the failed load was a staged full refresh).
    """
    return rewrite_dlt_advice(redact_project_text(project_root, str(error)), source_name, kept=kept)


def rich_error_text(
    project_root: Path | None,
    error: object,
    source_name: str | None = None,
    *,
    kept: bool = True,
) -> str:
    """Text of *error* with secrets masked and Rich markup escaped (for ``console.print``).

    The text is redacted first and cut afterwards (a cut can never expose part of a secret), to
    ``MAX_PRINTED_CHARS`` characters: nobody reads more than that on a terminal. The full text is
    still what stored error records keep. DuckDB's raw lock-conflict text is replaced by a plain
    explanation (the raw text goes to the debug log).
    """
    text = rewrite_lock_text(redacted_error_text(project_root, error, source_name, kept=kept))
    if len(text) > MAX_PRINTED_CHARS:
        text = f"{text[:MAX_PRINTED_CHARS]}... [truncated {len(text) - MAX_PRINTED_CHARS} chars]"
    return escape(text)
