"""scripts/check_audit_ignores.py

Validate the dated pip-audit ignore list (.github/pip-audit-ignores.txt).
Each entry is `ID  # reason, expires YYYY-MM-DD`. Fails (exit 1) when an entry is
expired, has no expiry date, or expires more than MAX_DAYS_AHEAD days from today.
"""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / ".github" / "pip-audit-ignores.txt"
MAX_DAYS_AHEAD = 90
_EXPIRES = re.compile(r"expires\s+(\d{4}-\d{2}-\d{2})\b")


def parse_entries(text: str) -> list[tuple[str, date]]:
    """Return (advisory id, expiry date) for each entry; raise ValueError on a bad line."""
    entries: list[tuple[str, date]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        vuln_id, _, comment = line.partition("#")
        vuln_id = vuln_id.strip()
        match = _EXPIRES.search(comment)
        if not vuln_id or " " in vuln_id or match is None:
            raise ValueError(f"line {number}: expected `ID  # reason, expires YYYY-MM-DD`")
        try:
            expires = date.fromisoformat(match.group(1))
        except ValueError as exc:
            raise ValueError(f"line {number}: invalid date {match.group(1)}") from exc
        entries.append((vuln_id, expires))
    return entries


def main(argv: list[str] | None = None, today: date | None = None) -> int:
    """Check the ignore list; return 0 when every entry is current."""
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else DEFAULT_PATH
    today = today or date.today()
    try:
        entries = parse_entries(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1

    problems = []
    latest = today + timedelta(days=MAX_DAYS_AHEAD)
    for vuln_id, expires in entries:
        if expires < today:
            problems.append(
                f"{vuln_id}: expired {expires} (re-test the advisory, then remove or renew)"
            )
        elif expires > latest:
            problems.append(f"{vuln_id}: expires {expires}, more than {MAX_DAYS_AHEAD} days out")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
