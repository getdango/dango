#!/usr/bin/env python3
"""scripts/doc_counts.py

Print the numbers that docs quote: the source-registry size and the line count of every file
named in the CLAUDE.md "Monolithic Files" table.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_ROW = re.compile(r"^\| `([^`]+\.(?:py|html))` \| (\d+) \|", re.MULTILINE)


def source_count() -> int:
    """Number of source types in SOURCE_REGISTRY."""
    sys.path.insert(0, str(REPO_ROOT))
    from dango.ingestion.sources.registry import SOURCE_REGISTRY

    return len(SOURCE_REGISTRY)


def monolithic_files() -> list[str]:
    """Paths (relative to dango/) listed in the CLAUDE.md Monolithic Files table."""
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    return [m.group(1) for m in _ROW.finditer(text)]


def line_counts() -> dict[str, int]:
    """Actual line count for each listed file."""
    counts = {}
    for rel in monolithic_files():
        path = REPO_ROOT / "dango" / rel
        counts[rel] = len(path.read_text(encoding="utf-8").splitlines())
    return counts


def main() -> int:
    """Print the counts."""
    print(f"sources: {source_count()}")
    for rel, n in line_counts().items():
        print(f"{n:6d}  dango/{rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
