"""scripts/check_bandit_baseline.py

Fail on Bandit findings not present in the occurrence-aware baseline.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".github" / "bandit-baseline.json"
_NUMBERED_LINE = re.compile(r"^\s*\d+\s+(.*)$")


def fingerprint(issue: dict[str, Any]) -> tuple[str, ...]:
    """Include source context so another occurrence in the same file is distinct."""
    code = issue["code"]
    lines = []
    for line in code.splitlines():
        match = _NUMBERED_LINE.match(line)
        lines.append((match.group(1) if match else line).strip())
    return (
        issue["filename"],
        issue["test_id"],
        issue["issue_severity"],
        issue["issue_confidence"],
        issue["issue_text"],
        "\n".join(lines),
    )


def new_findings(
    baseline: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Consume one baseline occurrence per matching current finding."""
    remaining = Counter(fingerprint(issue) for issue in baseline)
    added = []
    for issue in current:
        key = fingerprint(issue)
        if remaining[key]:
            remaining[key] -= 1
        else:
            added.append(issue)
    return added


def main() -> int:
    """Run the pinned Bandit scanner and compare its JSON with the baseline."""
    try:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        if baseline["errors"]:
            raise ValueError("baseline contains scan errors")
        previous = baseline["results"]
        if not isinstance(previous, list):
            raise ValueError("baseline results are invalid")
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"Bandit baseline is invalid: {exc}", file=sys.stderr)
        return 2

    result = subprocess.run(
        [sys.executable, "-m", "bandit", "-r", "dango", "-ll", "-ii", "-f", "json", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        print(f"Bandit scan failed (exit {result.returncode}): {result.stderr}", file=sys.stderr)
        return 2
    try:
        scan = json.loads(result.stdout)
        if scan["errors"]:
            raise ValueError("scan contains parse errors")
        current = scan["results"]
        if not isinstance(current, list):
            raise ValueError("scan results are invalid")
        added = new_findings(previous, current)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"Bandit scan output is invalid: {exc}", file=sys.stderr)
        return 2

    for issue in added:
        print(
            f"New Bandit finding: {issue['filename']}:{issue['line_number']} "
            f"{issue['test_id']} {issue['issue_severity']}/{issue['issue_confidence']} "
            f"{issue['issue_text']}"
        )
    print(f"Bandit: {len(current)} findings, {len(added)} new")
    return 1 if added else 0


if __name__ == "__main__":
    raise SystemExit(main())
