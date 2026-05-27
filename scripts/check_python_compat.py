"""scripts/check_python_compat.py

Check that all installed packages support Python 3.10, 3.11, and 3.12.
Uses package metadata (Requires-Python) to verify compatibility without
needing multiple Python interpreters installed.

Transitive dependencies that have dropped a Python version are flagged as
warnings (not failures) because pip resolves per-platform — on Python 3.10
it would install an older compatible version automatically.

Exit codes:
    0 — all direct deps compatible (transitive warnings are OK)
    1 — a direct dependency dropped support for a required Python version
"""

import importlib.metadata
import re
import sys
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

# Python versions that getdango must support (see pyproject.toml requires-python)
REQUIRED_VERSIONS = [Version("3.10.0"), Version("3.11.0"), Version("3.12.0")]

# Packages to skip (not real PyPI packages)
SKIP_PACKAGES = {"getdango", "en-core-web-sm", "en-core-web-lg"}


def _get_direct_dep_names() -> set[str]:
    """Extract direct dependency names from pyproject.toml.

    Uses string parsing (not tomllib) so the script works on Python 3.10.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")

    # Extract lines between `dependencies = [` and the closing `]`
    names: set[str] = set()
    in_deps = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "dependencies = [":
            in_deps = True
            continue
        if in_deps and stripped == "]":
            break
        if not in_deps:
            continue
        # Extract quoted dependency string
        match = re.match(r'\s*"([^"]+)"', line)
        if not match:
            continue
        dep_str = match.group(1)
        # Package name is everything before the first version specifier or bracket
        name = re.split(r"[>=<~!\[]", dep_str)[0].strip()
        if name:
            names.add(_normalize(name))
    return names


def _normalize(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def main() -> int:
    """Check all installed packages for Python version compatibility."""
    direct_deps = _get_direct_dep_names()

    direct_failures = []
    transitive_warnings = []
    checked = 0
    skipped = 0

    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if name in SKIP_PACKAGES:
            continue

        requires_python = dist.metadata.get("Requires-Python")
        if not requires_python:
            skipped += 1
            continue

        checked += 1
        spec = SpecifierSet(requires_python)
        unsupported = [v for v in REQUIRED_VERSIONS if v not in spec]

        if unsupported:
            versions_str = ", ".join(str(v) for v in unsupported)
            entry = (name, dist.metadata["Version"], requires_python, versions_str)
            if _normalize(name) in direct_deps:
                direct_failures.append(entry)
            else:
                transitive_warnings.append(entry)

    print(f"Checked {checked} packages ({skipped} without Requires-Python metadata)")

    if transitive_warnings:
        print(
            f"\nWARN: {len(transitive_warnings)} transitive package(s) installed at versions "
            "that drop a target Python (pip resolves per-platform, so this is informational):"
        )
        for name, version, spec, unsupported in sorted(transitive_warnings):
            print(f"  {name}=={version} (requires {spec}) — drops Python {unsupported}")

    if direct_failures:
        print(f"\nFAIL: {len(direct_failures)} direct dependency(s) incompatible:")
        for name, version, spec, unsupported in sorted(direct_failures):
            print(f"  {name}=={version} (requires {spec}) — drops Python {unsupported}")
        return 1

    print("\nOK: All direct dependencies support Python 3.10, 3.11, and 3.12")
    return 0


if __name__ == "__main__":
    sys.exit(main())
