"""scripts/check_orphan_files.py

Find Python files >100 lines under dango/ not mentioned in any CLAUDE.md.
CI-only check. Always exits 0 (warning only).
"""

import os
import re
import sys

SKIP_DIRS = {"venv", ".venv", "__pycache__", ".git", "dlt_sources", "node_modules"}
MIN_LINES = 100


def _count_lines(path: str) -> int:
    """Count lines in a file."""
    with open(path, encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


def _find_python_files() -> list[tuple[str, int]]:
    """Find Python files >MIN_LINES under dango/, returning (path, line_count)."""
    results = []
    for root, dirs, filenames in os.walk("dango"):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in filenames:
            if fn == "__init__.py":
                continue
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            line_count = _count_lines(path)
            if line_count > MIN_LINES:
                results.append((path.replace(os.sep, "/"), line_count))
    return results


def _find_claude_md_files() -> list[str]:
    """Find all CLAUDE.md files in the repo."""
    results = []
    for root, dirs, filenames in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in filenames:
            if fn == "CLAUDE.md":
                results.append(os.path.join(root, fn))
    return results


def _extract_filenames_from_claude_md(path: str) -> set[str]:
    """Extract .py filenames mentioned in a CLAUDE.md file."""
    filenames = set()
    with open(path, encoding="utf-8") as f:
        content = f.read()

    # Single regex catches all .py references (table rows, backticks, bare)
    for match in re.finditer(r"(\S+\.py)", content):
        filenames.add(match.group(1).strip("`"))

    return filenames


def _is_mentioned(path: str, claude_md_path: str, mentioned: set[str]) -> bool:
    """Check if a file is mentioned in the CLAUDE.md that owns its module."""
    # Only count mentions from the CLAUDE.md in the same module directory
    module_dir = os.path.dirname(path)
    claude_dir = os.path.dirname(claude_md_path).replace(os.sep, "/").lstrip("./")
    if not module_dir.startswith(claude_dir):
        return False
    basename = os.path.basename(path)
    return basename in mentioned or path in mentioned


def main() -> int:
    """Detect Python files not referenced in any CLAUDE.md."""
    py_files = _find_python_files()
    if not py_files:
        print("No Python files >100 lines found.")
        return 0

    # Collect mentions per CLAUDE.md file
    claude_md_mentions: list[tuple[str, set[str]]] = []
    for claude_md in _find_claude_md_files():
        claude_md_mentions.append((claude_md, _extract_filenames_from_claude_md(claude_md)))

    # Check which files are orphans
    orphans = []
    for path, line_count in py_files:
        found = any(
            _is_mentioned(path, claude_md, mentioned) for claude_md, mentioned in claude_md_mentions
        )
        if not found:
            orphans.append((path, line_count))

    if orphans:
        print(f"WARNING: {len(orphans)} Python file(s) >100 lines not in any CLAUDE.md:")
        for path, line_count in sorted(orphans):
            print(f"  {path} ({line_count} lines)")
    else:
        print(f"All {len(py_files)} Python files >100 lines are documented in CLAUDE.md files.")

    return 0  # Always exit 0 (warning only)


if __name__ == "__main__":
    sys.exit(main())
