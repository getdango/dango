"""scripts/check_file_sizes.py

Check Python file sizes against project limits.
Pre-commit mode: warn >300 lines, fail >500 lines.
CI mode (--all): fail >500 lines.
"""

import argparse
import os
import sys

# TODO(TASK-084): Move exemptions to docs/file-exemptions.yml
EXEMPT_FILES = {
    # Cataloged in MEMORY.md (known technical debt)
    "dango/ingestion/dlt_runner.py",
    "dango/ingestion/sources/registry.py",
    "dango/visualization/metabase.py",
    "dango/visualization/dashboard_manager.py",
    "dango/ingestion/csv_loader.py",
    "dango/oauth/providers.py",
    "dango/transformation/generator.py",
    # Additional MVP files over 500 lines
    "dango/cli/init.py",
    "dango/cli/main.py",
    "dango/cli/model_wizard.py",
    "dango/cli/source_wizard.py",
    "dango/cli/utils.py",
    "dango/cli/validate.py",
    "dango/platform/network.py",
    "dango/platform/watcher.py",
    "dango/web/app.py",
}

SKIP_DIRS = {"venv", ".venv", "__pycache__", ".git", "dlt_sources", "node_modules"}
WARN_THRESHOLD = 300
FAIL_THRESHOLD = 500


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


def check_files(files: list[str], audit_mode: bool = False) -> int:
    """Check file sizes. Returns exit code."""
    warnings = []
    failures = []

    for path in files:
        if _should_skip(path):
            continue
        if not os.path.isfile(path):
            continue

        rel_path = _normalize_path(path)
        if rel_path in EXEMPT_FILES:
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
        exempt_count = sum(1 for f in checked if _normalize_path(f) in EXEMPT_FILES)
        total = len(checked) - exempt_count
        print(f"All files within size limits ({total} checked, {exempt_count} exempt).")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Python file sizes")
    parser.add_argument("files", nargs="*", help="Files to check (pre-commit mode)")
    parser.add_argument("--all", action="store_true", help="Check all Python files (CI mode)")
    args = parser.parse_args()

    if args.all:
        files = _find_all_python_files()
        return check_files(files, audit_mode=True)
    elif args.files:
        return check_files(args.files, audit_mode=False)
    else:
        parser.error("Provide file paths or use --all")
        return 1


if __name__ == "__main__":
    sys.exit(main())
