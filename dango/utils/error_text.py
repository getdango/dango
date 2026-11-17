"""dango/utils/error_text.py

One place that turns error text into something safe to print: secrets redacted, Rich markup escaped.
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape

from dango.utils.redact import redact_project_text


def redacted_error_text(project_root: Path | None, error: object) -> str:
    """Plain text of *error* with secrets masked (for logs and stored text)."""
    return redact_project_text(project_root, str(error))


def rich_error_text(project_root: Path | None, error: object) -> str:
    """Text of *error* with secrets masked and Rich markup escaped (for ``console.print``)."""
    return escape(redacted_error_text(project_root, error))
