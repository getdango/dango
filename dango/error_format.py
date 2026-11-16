"""dango/error_format.py

Structured what/why/fix error message formatting (re-exported by ``dango.exceptions``).
"""

from __future__ import annotations


def format_structured_error(
    what_failed: str,
    causes: list[str],
    suggested_fix: str,
) -> str:
    """Format a structured error message with what/why/fix sections.

    Args:
        what_failed: One-line description of the failure.
        causes: Possible root causes (rendered as a bullet list).
        suggested_fix: Actionable next step for the user.

    Returns:
        Multi-line string with three sections: what, causes, fix.
    """
    lines = [what_failed, "", "Possible causes:"]
    for cause in causes:
        lines.append(f"  - {cause}")
    lines.append("")
    lines.append(f"Suggested fix: {suggested_fix}")
    return "\n".join(lines)
