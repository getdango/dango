"""dango/utils/spacy_model.py

The message shown when the spaCy model for the PII scan is missing and could not be installed.

Level 0 module. Plain ASCII; the manual command uses the interpreter Dango runs in.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from typing import Any

from rich.markup import escape

SPACY_MODEL = "en_core_web_sm"


def spacy_install_command() -> str:
    """The manual install command for the interpreter that runs Dango (quoted if needed)."""
    argv = [sys.executable or "python", "-m", "spacy", "download", SPACY_MODEL]
    if sys.platform == "win32":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def spacy_unavailable_lines(reason: str = "") -> list[str]:
    """Plain ASCII lines: what is skipped, the exact command, and that the rest still works."""
    first = f"PII scan skipped: the spaCy language model {SPACY_MODEL} is not available"
    first += f" ({reason})." if reason else "."
    return [
        first,
        f"To enable it, run: {spacy_install_command()}",
        "The rest of Dango works as normal; the PII scan runs again once the model is installed.",
    ]


def print_spacy_unavailable(reason: str = "", console: Any = None) -> None:
    """Print the message (yellow, Rich-escaped) to stderr, or to *console*."""
    if console is None:
        from rich.console import Console

        console = Console(stderr=True)
    for line in spacy_unavailable_lines(reason):
        console.print(f"[yellow]{escape(line)}[/yellow]", soft_wrap=True)
