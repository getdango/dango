"""scripts/check_pinned_deps.py

Check that all dependencies in pyproject.toml have version constraints.
CI-only check. Always exits 0 (warning only).
"""

import re
import sys

PYPROJECT_PATH = "pyproject.toml"
VERSION_PATTERN = re.compile(r"[><=~!]")


def main() -> int:
    try:
        with open(PYPROJECT_PATH, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"ERROR: {PYPROJECT_PATH} not found.")
        return 0

    # Parse dependencies from [project.dependencies]
    in_deps = False
    unpinned = []

    for line in content.splitlines():
        stripped = line.strip()

        if stripped == "dependencies = [":
            in_deps = True
            continue
        if in_deps and stripped == "]":
            break
        if not in_deps:
            continue

        # Extract dependency string from quoted line
        match = re.search(r'"([^"]+)"', stripped)
        if not match:
            continue

        dep_str = match.group(1)
        # Extract package name (before any version specifier or extras)
        pkg_name = re.split(r"[><=~!\[;]", dep_str)[0].strip()

        if not VERSION_PATTERN.search(dep_str):
            unpinned.append(pkg_name)

    if unpinned:
        print(f"WARNING: {len(unpinned)} dependency(ies) without version constraints:")
        for pkg in unpinned:
            print(f"  {pkg}")
    else:
        print("All dependencies have version constraints.")

    return 0  # Always exit 0 (warning only)


if __name__ == "__main__":
    sys.exit(main())
