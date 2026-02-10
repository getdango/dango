"""scripts/check_file_sizes.py

Check Python file sizes against project limits.
Pre-commit mode: warn >300 lines, fail >500 lines.
CI mode (--all): fail >500 lines, detect stale exemptions.

Exemptions loaded from docs/file-exemptions.yml (TASK-084).
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

EXEMPTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "docs",
    "file-exemptions.yml",
)

SKIP_DIRS = {"venv", ".venv", "__pycache__", ".git", "dlt_sources", "node_modules"}
WARN_THRESHOLD = 300
FAIL_THRESHOLD = 500


def _load_exemptions(path: str) -> set[str]:
    """Load exempt file paths from YAML. Returns empty set on failure."""
    if yaml is None:
        print(f"WARNING: PyYAML not installed, cannot load exemptions from {path}")
        return set()
    if not os.path.isfile(path):
        print(f"WARNING: Exemptions file not found: {path}")
        return set()
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or not isinstance(data.get("exemptions"), list):
        print(f"WARNING: No exemptions found in {path}")
        return set()
    return {entry["file"] for entry in data["exemptions"] if "file" in entry}


def _normalize_path(path: str) -> str:
    """Normalize path to use forward slashes relative to repo root."""
    abs_path = os.path.abspath(path)
    # Try to find repo root by looking for pyproject.toml
    candidate = os.path.dirname(abs_path)
    while candidate != os.path.dirname(candidate):
        if os.path.exists(os.path.join(candidate, "pyproject.toml")):
            return os.path.relpath(abs_path, candidate).replace(os.sep, "/")
        candidate = os.path.dirname(candidate)
    return path.replace(os.sep, "/")


def _should_skip(path: str) -> bool:
    """Check if file should be skipped."""
    parts = path.replace(os.sep, "/").split("/")
    if any(part in SKIP_DIRS for part in parts):
        return True
    if os.path.basename(path) == "__init__.py":
        return True
    return False


def _count_lines(path: str) -> int:
    """Count lines in a file."""
    with open(path, encoding="utf-8", errors="replace") as f:
        return sum(1 for line in f)


def _find_all_python_files() -> list[str]:
    """Find all Python files under dango/ and scripts/."""
    files = []
    for search_dir in ["dango", "scripts", "tests"]:
        if not os.path.isdir(search_dir):
            continue
        for root, dirs, filenames in os.walk(search_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(".py"):
                    files.append(os.path.join(root, fn))
    return files


def _check_stale_exemptions(exempt_files: set[str]) -> None:
    """Print notices for exemptions that are stale (file missing or now <=limit)."""
    stale = []
    for file_path in sorted(exempt_files):
        if not os.path.isfile(file_path):
            stale.append((file_path, "file not found"))
        else:
            line_count = _count_lines(file_path)
            if line_count <= FAIL_THRESHOLD:
                stale.append((file_path, f"now {line_count} lines (within limit)"))
    if stale:
        print(f"\nNOTICE: {len(stale)} stale exemption(s):")
        for path, reason in stale:
            print(f"  {path}: {reason}")
        print("  Consider removing from docs/file-exemptions.yml\n")


def check_files(
    files: list[str], audit_mode: bool = False, exempt_files: set[str] | None = None
) -> int:
    """Check file sizes. Returns exit code."""
    if exempt_files is None:
        exempt_files = set()

    warnings = []
    failures = []

    for path in files:
        if _should_skip(path):
            continue
        if not os.path.isfile(path):
            continue

        rel_path = _normalize_path(path)
        if rel_path in exempt_files:
            continue

        line_count = _count_lines(path)

        if line_count > FAIL_THRESHOLD:
            failures.append((rel_path, line_count))
        elif line_count > WARN_THRESHOLD and not audit_mode:
            warnings.append((rel_path, line_count))

    if warnings:
        print(f"WARNING: {len(warnings)} file(s) exceed {WARN_THRESHOLD} lines:")
        for path, count in sorted(warnings):
            print(f"  {path}: {count} lines")

    if failures:
        print(f"FAIL: {len(failures)} file(s) exceed {FAIL_THRESHOLD} lines:")
        for path, count in sorted(failures):
            print(f"  {path}: {count} lines")
        return 1

    if not warnings and not failures:
        checked = [f for f in files if not _should_skip(f) and os.path.isfile(f)]
        exempt_count = sum(1 for f in checked if _normalize_path(f) in exempt_files)
        total = len(checked) - exempt_count
        print(f"All files within size limits ({total} checked, {exempt_count} exempt).")

    return 0


def main() -> int:
    """Entry point for file size checking (CLI and pre-commit)."""
    parser = argparse.ArgumentParser(description="Check Python file sizes")
    parser.add_argument("files", nargs="*", help="Files to check (pre-commit mode)")
    parser.add_argument("--all", action="store_true", help="Check all Python files (CI mode)")
    args = parser.parse_args()

    exempt_files = _load_exemptions(EXEMPTIONS_PATH)

    if args.all:
        files = _find_all_python_files()
        result = check_files(files, audit_mode=True, exempt_files=exempt_files)
        _check_stale_exemptions(exempt_files)
        return result
    elif args.files:
        return check_files(args.files, audit_mode=False, exempt_files=exempt_files)
    else:
        parser.error("Provide file paths or use --all")
        return 1


if __name__ == "__main__":
    sys.exit(main())
