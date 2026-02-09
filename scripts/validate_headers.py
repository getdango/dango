#!/usr/bin/env python3
"""
Validate Python file header docstrings.

Checks that Python files begin with a docstring containing:
1. A file path line (e.g., "dango/config/loader.py")
2. A purpose line (non-empty text after the path)

Usage:
    python scripts/validate_headers.py dango/config/loader.py
    python scripts/validate_headers.py dango/config/loader.py dango/utils/database.py
    python scripts/validate_headers.py --changed   # git diff against base branch
    python scripts/validate_headers.py --all        # audit all .py files
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Directories to skip in --all mode
SKIP_DIRS = {"__pycache__", ".git", "venv", ".venv", "node_modules", ".tox", ".eggs"}

# Files to skip (auto-generated or trivially empty)
SKIP_FILES = {"__init__.py"}


def get_repo_root() -> Path:
    """Find the git repository root."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


def get_changed_python_files() -> list[Path]:
    """Get Python files changed relative to the base branch (v1 or main)."""
    repo_root = get_repo_root()

    # Try v1 first (our integration branch), fall back to main
    for base in ("v1", "origin/v1", "main", "origin/main"):
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=ACM", base, "HEAD"],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo_root,
            )
            files = [
                repo_root / f.strip()
                for f in result.stdout.strip().split("\n")
                if f.strip().endswith(".py")
            ]
            return [f for f in files if f.exists()]
        except subprocess.CalledProcessError:
            continue

    # If no base branch found, check staged + unstaged changes
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACM", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=repo_root,
        )
        files = [
            repo_root / f.strip()
            for f in result.stdout.strip().split("\n")
            if f.strip().endswith(".py")
        ]
        return [f for f in files if f.exists()]
    except subprocess.CalledProcessError:
        return []


def find_all_python_files(root: Path) -> list[Path]:
    """Find all Python files under root, excluding skipped dirs and files."""
    results = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if any(p in SKIP_DIRS for p in parts):
            continue
        if path.name in SKIP_FILES:
            continue
        results.append(path)
    return results


def validate_header(file_path: Path) -> list[str]:
    """
    Validate a Python file's header docstring.

    Args:
        file_path: Path to the Python file

    Returns:
        List of error messages (empty if valid)
    """
    errors: list[str] = []

    if not file_path.exists():
        return [f"File not found: {file_path}"]

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"Cannot read file (encoding error): {file_path}"]

    # Strip leading whitespace/blank lines
    stripped = content.lstrip()

    if not stripped:
        return ["File is empty"]

    # Must start with a docstring
    if not stripped.startswith('"""'):
        errors.append("File does not start with a docstring (expected triple-quoted \"\"\")")
        return errors

    # Extract the docstring content
    # Find the closing """
    docstring_start = stripped.index('"""') + 3
    closing_idx = stripped.find('"""', docstring_start)
    if closing_idx == -1:
        errors.append("Docstring is not closed (missing closing \"\"\")")
        return errors

    docstring_body = stripped[docstring_start:closing_idx].strip()
    lines = [line.strip() for line in docstring_body.split("\n")]

    # Remove empty lines at start
    while lines and not lines[0]:
        lines.pop(0)

    if not lines:
        errors.append("Docstring is empty")
        return errors

    # First non-empty line should be a file path (contains / and ends with .py)
    first_line = lines[0]
    if not re.match(r"^[\w./-]+\.py$", first_line):
        errors.append(
            f"First line of docstring should be a file path (got: '{first_line}')"
        )

    # Must have a purpose line (non-empty line after the path, not a section header)
    remaining = [l for l in lines[1:] if l]
    purpose_found = False
    for line in remaining:
        # Skip section headers like "Related files:" or "Entry points:"
        if line.endswith(":") or line.startswith("- "):
            continue
        # This is a purpose line
        purpose_found = True
        break

    if not purpose_found:
        errors.append("Docstring missing a purpose line after the file path")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Python file header docstrings."
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Python file(s) to validate",
    )
    parser.add_argument(
        "--changed",
        action="store_true",
        help="Validate only files changed relative to base branch",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Audit all Python files (report compliance stats)",
    )
    args = parser.parse_args()

    if not args.files and not args.changed and not args.all:
        parser.error("Provide file paths, --changed, or --all")

    # Collect files
    files: list[Path] = []
    audit_mode = False

    if args.all:
        audit_mode = True
        # Look for dango/ package and tests/ directory
        root = Path.cwd()
        files = find_all_python_files(root / "dango")
        test_dir = root / "tests"
        if test_dir.exists():
            files.extend(find_all_python_files(test_dir))
        if not files:
            print("No Python files found.")
            return 0
    elif args.changed:
        files = get_changed_python_files()
        if not files:
            print("No changed Python files found.")
            return 0
    else:
        files = args.files

    # Validate
    valid_count = 0
    invalid_count = 0
    file_errors: list[tuple[Path, list[str]]] = []

    for file_path in files:
        errors = validate_header(file_path)
        if errors:
            invalid_count += 1
            file_errors.append((file_path, errors))
        else:
            valid_count += 1

    # Report
    total = valid_count + invalid_count

    if audit_mode:
        print(f"Header compliance: {valid_count}/{total} files ({valid_count * 100 // total}%)")
        if file_errors:
            print(f"\nFiles missing headers ({invalid_count}):")
            for file_path, errors in file_errors:
                print(f"  {file_path}")
        return 0  # Audit mode always exits 0

    # Normal mode — show errors and fail if any
    for file_path, errors in file_errors:
        print(f"\n{file_path}:")
        for error in errors:
            print(f"  - {error}")

    if valid_count > 0 and not file_errors:
        print(f"\nAll {total} file(s) have valid headers.")

    if file_errors:
        print(f"\n{invalid_count} of {total} file(s) failed validation.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
