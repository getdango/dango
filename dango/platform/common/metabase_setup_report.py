"""dango/platform/common/metabase_setup_report.py

The lines `dango start` prints when Metabase setup did not finish: error text escaped and redacted,
and a closing message that matches what actually happened (warehouse connected or not).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rich.markup import escape

from dango.utils.duckdb_lock_text import rewrite_lock_text
from dango.utils.error_text import redacted_error_text

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
    succeed (``duckdb_connected`` false or absent) the message says Metabase is running but not
    connected, that nothing is lost and that running ``dango start`` again retries; it never claims
    the connection. The wording is plain ASCII (the status marks are the ones the command already
    printed).
    """
    errors = [e for e in (setup_result.get("errors") or []) if e]
    connected = bool(setup_result.get("duckdb_connected"))
    lines = ["[red]✗[/red] Metabase setup failed"]
    lines.extend(f"[red]  • {_printable(project_root, e)}[/red]" for e in errors[:MAX_ERROR_ITEMS])
    if len(errors) > MAX_ERROR_ITEMS:
        lines.append(f"[red]  • ... and {len(errors) - MAX_ERROR_ITEMS} more[/red]")
    if any("timed out" in str(e).lower() for e in errors):
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
            "[yellow]⚠ Metabase is running, but Dango could not finish connecting it "
            "to the warehouse.[/yellow]"
        )
        lines.append(
            "[dim]  Nothing is lost: running dango start again is safe and retries "
            "the connection.[/dim]"
        )
    lines.append(
        f"[dim]  You can manually complete setup at http://localhost:{metabase_port}[/dim]"
    )
    return lines
