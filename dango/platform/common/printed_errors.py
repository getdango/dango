"""dango/platform/common/printed_errors.py

How the error items of a Metabase setup, sync, save, load or provision result are printed: each
item redacted, flattened to one line, cut, and (for Rich output) escaped, with a cap on the count.
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.markup import escape

from dango.utils.duckdb_lock_text import rewrite_lock_text
from dango.utils.error_text import redacted_error_text

# One printed error line is cut here (the redaction runs first, so a cut never exposes a secret).
MAX_ERROR_CHARS = 500
# More error items than this are summarised in one line.
MAX_ERROR_ITEMS = 8

# Control characters (ESC, newline, tab, carriage return, C1) and Unicode line separators.
_CONTROL_RUN = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def error_items(errors: object) -> list[object]:
    """The printable error items of a result's ``errors`` value.

    A list, tuple or set gives its non-empty items; a string, mapping or other single value is one
    item; ``None`` and empty values give none.
    """
    if not errors:
        return []
    if isinstance(errors, (list, tuple, set, frozenset)):
        return [e for e in errors if e]
    return [errors]


def bounded_error_text(project_root: Path | None, error: object) -> str:
    """One error item as plain single-line text: redacted, flattened, then cut to ``MAX_ERROR_CHARS``."""
    text = rewrite_lock_text(redacted_error_text(project_root, error))
    text = _CONTROL_RUN.sub(" ", text)
    if len(text) > MAX_ERROR_CHARS:
        text = f"{text[:MAX_ERROR_CHARS]}... [truncated {len(text) - MAX_ERROR_CHARS} chars]"
    return text


def printable_error_text(project_root: Path | None, error: object) -> str:
    """One error item as Rich-safe text: ``bounded_error_text`` escaped last."""
    return escape(bounded_error_text(project_root, error))


def error_lines(project_root: Path | None, errors: object, *, rich: bool = True) -> list[str]:
    """One line per error item (at most ``MAX_ERROR_ITEMS``, then one "... and N more" line).

    ``rich=True`` lines are safe inside ``console.print`` markup; ``rich=False`` lines are plain
    text for ``print``.
    """
    items = error_items(errors)
    one = printable_error_text if rich else bounded_error_text
    lines = [one(project_root, e) for e in items[:MAX_ERROR_ITEMS]]
    if len(items) > MAX_ERROR_ITEMS:
        lines.append(f"... and {len(items) - MAX_ERROR_ITEMS} more")
    return lines
