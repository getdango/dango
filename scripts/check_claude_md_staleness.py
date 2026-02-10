"""scripts/check_claude_md_staleness.py

Warn when Python files are modified but the module's CLAUDE.md is not staged.
Pre-commit only. Always exits 0 (warning only).
"""

from __future__ import annotations

import os
import sys


def _get_module_dir(path: str) -> str | None:
    """Get the module directory for a Python file under dango/."""
    parts = path.replace(os.sep, "/").split("/")
    # We care about files under dango/<module>/
    if len(parts) < 3 or parts[0] != "dango":
        return None
    # Module is the second component: dango/<module>/...
    return f"{parts[0]}/{parts[1]}"


def main() -> int:
    """Warn when Python files change without updating their module CLAUDE.md."""
    files = sys.argv[1:]
    if not files:
        return 0

    # Group .py files by module, track which CLAUDE.md files are staged
    modules_touched: dict[str, list[str]] = {}
    claude_mds_staged: set[str] = set()

    for path in files:
        normalized = path.replace(os.sep, "/")
        if normalized.endswith("CLAUDE.md"):
            # Track the directory of the staged CLAUDE.md
            claude_mds_staged.add(os.path.dirname(normalized))
            continue
        if not normalized.endswith(".py"):
            continue

        module_dir = _get_module_dir(normalized)
        if module_dir:
            modules_touched.setdefault(module_dir, []).append(normalized)

    # Check each touched module
    warnings = []
    for module_dir, py_files in modules_touched.items():
        claude_md_path = os.path.join(module_dir, "CLAUDE.md")
        # Only warn if CLAUDE.md exists but wasn't staged
        if os.path.exists(claude_md_path) and module_dir not in claude_mds_staged:
            warnings.append((module_dir, py_files))

    if warnings:
        for module_dir, py_files in sorted(warnings):
            print(f"WARNING: Modified {module_dir}/ files but {module_dir}/CLAUDE.md not staged:")
            for f in sorted(py_files):
                print(f"  {f}")
            print(f"  Consider updating {module_dir}/CLAUDE.md")
        print()

    return 0  # Always exit 0 (warning only)


if __name__ == "__main__":
    sys.exit(main())
