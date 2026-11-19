"""dango/utils/build_output.py

Printable summary of a failed Docker image build: last lines (redacted, markup-escaped) and a DNS hint.

Level 0 module. Every line returned is safe to pass to ``console.print``.
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.markup import escape

from dango.utils.redact import redact_project_text

DEFAULT_MAX_LINES = 30
MAX_LINE_CHARS = 500

_DNS_RE = re.compile(
    r"temporary failure in name resolution|could not resolve|no such host|dial tcp: lookup",
    re.IGNORECASE,
)

_DNS_HINT_LINES = (
    "The build could not resolve a host name, which usually points to a DNS problem.",
    "  - Check that this machine is online, then set a working DNS server in Docker's",
    "    daemon settings (the 'dns' key in daemon.json, or Settings > Docker Engine in",
    "    Docker Desktop) and restart Docker.",
    "  - To see whether the host's own network works, run this as a diagnostic only:",
    "    docker build --network host -f Dockerfile.metabase .",
)


def looks_like_dns_failure(text: str) -> bool:
    """True when *text* shows a name-resolution failure."""
    return _DNS_RE.search(text) is not None


def build_failure_report(
    project_root: Path | None, output: str, max_lines: int = DEFAULT_MAX_LINES
) -> str:
    """Last *max_lines* lines of *output*, redacted and escaped, plus the DNS hint when it applies."""
    redacted = redact_project_text(project_root, output)
    lines = [line for line in redacted.splitlines() if line.strip()]
    tail = [
        line if len(line) <= MAX_LINE_CHARS else f"{line[:MAX_LINE_CHARS]}... [cut]"
        for line in lines[-max_lines:]
    ]
    parts: list[str] = []
    if tail:
        parts.append(f"Last {len(tail)} line(s) of build output:")
        parts.extend(escape(line) for line in tail)
    if looks_like_dns_failure(redacted):
        parts.append("")
        parts.extend(escape(line) for line in _DNS_HINT_LINES)
    return "\n".join(parts)
