"""scripts/check_version_alignment.py

Pre-commit hook that verifies Python DuckDB and Metabase JDBC driver
share the same major.minor version.

Parses pyproject.toml and dango/utils/driver.py directly — no dango
imports required (runs outside venv with system Python).

Exit codes: 0 = aligned, 1 = mismatch or parse failure.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _extract_pyproject_duckdb_major_minor() -> str | None:
    """Extract DuckDB major.minor from pyproject.toml dependency spec."""
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return None

    text = pyproject.read_text()

    # Match patterns: "duckdb>=X.Y.Z,<A.B", "duckdb==X.Y.Z", "duckdb~=X.Y.Z"
    match = re.search(r'"duckdb([>~=!]+)(\d+\.\d+)\.\d+', text)
    if match:
        return match.group(2)

    return None


def _extract_driver_major_minor() -> str | None:
    """Extract METABASE_DUCKDB_DRIVER_VERSION major.minor from driver.py."""
    driver_py = REPO_ROOT / "dango" / "utils" / "driver.py"
    if not driver_py.exists():
        return None

    text = driver_py.read_text()

    match = re.search(r'METABASE_DUCKDB_DRIVER_VERSION\s*=\s*"(\d+\.\d+)\.\d+', text)
    if match:
        return match.group(1)

    return None


def main() -> int:
    """Check version alignment, return 0 on success, 1 on failure."""
    pyproject_mm = _extract_pyproject_duckdb_major_minor()
    driver_mm = _extract_driver_major_minor()

    if pyproject_mm is None:
        print("ERROR: Could not parse DuckDB version from pyproject.toml", file=sys.stderr)
        return 1

    if driver_mm is None:
        print(
            "ERROR: Could not parse METABASE_DUCKDB_DRIVER_VERSION from dango/utils/driver.py",
            file=sys.stderr,
        )
        return 1

    if pyproject_mm != driver_mm:
        print(
            f"ERROR: DuckDB version mismatch!\n"
            f"  pyproject.toml duckdb: {pyproject_mm}.x\n"
            f"  driver.py METABASE_DUCKDB_DRIVER_VERSION: {driver_mm}.x\n"
            f"  These must share the same major.minor — DuckDB read-only mode\n"
            f"  cannot cross major.minor versions.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
