"""dango/platform/common/metabase_setup_report.py

The lines `dango start` prints when Metabase setup did not finish: error text escaped and redacted,
and a closing message that matches what actually happened (warehouse connected or not).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rich.markup import escape

from dango.utils.duckdb_lock_text import rewrite_lock_text
from dango.utils.error_text import redacted_error_text

# The HTTP client's own timeout wording (requests/urllib3). Metabase's own "Timed out after ..." reply
# is not matched: Metabase did answer in that case.
_CLIENT_TIMEOUT = re.compile(r"read timed out|\((?:read|connect) timeout=", re.IGNORECASE)

# One printed error line is cut here (the redaction runs first, so a cut never exposes a secret).
MAX_ERROR_CHARS = 500
# More error items than this are summarised in one line.
MAX_ERROR_ITEMS = 8


def _printable(project_root: Path | None, error: object) -> str:
    """One error item as Rich-safe text: redacted, cut to ``MAX_ERROR_CHARS``, then escaped."""
    text = rewrite_lock_text(redacted_error_text(project_root, error))
    if len(text) > MAX_ERROR_CHARS:
        text = f"{text[:MAX_ERROR_CHARS]}... [truncated {len(text) - MAX_ERROR_CHARS} chars]"
    return escape(text)


def metabase_setup_failure_lines(
    project_root: Path | None,
    setup_result: Mapping[str, Any],
    metabase_port: int,
) -> list[str]:
    """Rich-markup lines for a Metabase setup that ended without success.

    Every error item is printed redacted and escaped. When the warehouse connection did not
    succeed (``duckdb_connected`` false or absent) the message says setup did not finish and Metabase
    is not connected to the warehouse yet, that nothing is lost and that running ``dango start``
    again retries the setup (it does not say Metabase is running: the same flag is false when Metabase
    never became ready); it never claims the connection. The wording is plain ASCII (the status marks are the ones the command already
    printed).
    """
    errors = [e for e in (setup_result.get("errors") or []) if e]
    connected = bool(setup_result.get("duckdb_connected"))
    lines = ["[red]✗[/red] Metabase setup failed"]
    lines.extend(f"[red]  • {_printable(project_root, e)}[/red]" for e in errors[:MAX_ERROR_ITEMS])
    if len(errors) > MAX_ERROR_ITEMS:
        lines.append(f"[red]  • ... and {len(errors) - MAX_ERROR_ITEMS} more[/red]")
    if any(_CLIENT_TIMEOUT.search(str(e)) for e in errors):
        lines.append(
            "[dim]  The request to Metabase timed out: Metabase did not answer in time.[/dim]"
        )
    lines.append("")
    if connected:
        lines.append(
            "[yellow]⚠ Metabase partially configured (DuckDB is connected, "
            "but setup did not finish)[/yellow]"
        )
    else:
        lines.append(
            "[yellow]⚠ Metabase setup did not finish, so Metabase is not connected "
            "to the warehouse yet.[/yellow]"
        )
        lines.append(
            "[dim]  Nothing is lost: running dango start again is safe and retries the setup. "
            "If an error above names a command to run, run that first.[/dim]"
        )
    lines.append(
        f"[dim]  You can manually complete setup at http://localhost:{metabase_port}[/dim]"
    )
    return lines
