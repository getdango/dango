"""dango/cli/sync_report.py

Failed-source reporting for `dango sync` (names and redacted reasons, never a traceback).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.markup import escape

from dango.cli import console
from dango.utils.error_text import rich_error_text


def print_failed_sources(summary: dict[str, Any], project_root: Path) -> None:
    """Print each failed source with its redacted reason (plain ASCII markers)."""
    failed = summary.get("failed_sources") or []
    console.print()
    console.print(
        f"[red]Sync failed for {len(failed) or summary.get('failed_count', 0)} source(s):[/red]"
    )
    for item in failed:
        name = item.get("name", "unknown") if isinstance(item, dict) else str(item)
        reason = item.get("error") if isinstance(item, dict) else None
        text = rich_error_text(
            project_root, reason or "Unknown error", None if name == "unknown" else str(name)
        )
        console.print(f"  - {escape(str(name))}: {text}")
