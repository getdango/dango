"""scripts/check_docstrings.py

Check that public functions and classes have docstrings.
Pre-commit mode: accepts file paths, exits 1 on violations.
CI mode (--all): audit all files, exits 0 with report.
"""

import argparse
import ast
import os
import sys

SKIP_DIRS = {"venv", ".venv", "__pycache__", ".git", "dlt_sources", "node_modules"}


def _should_skip(path: str) -> bool:
    """Check if file should be skipped."""
    parts = path.replace(os.sep, "/").split("/")
    if any(part in SKIP_DIRS for part in parts):
        return True
    basename = os.path.basename(path)
    if basename == "__init__.py":
        return True
    # Skip test files
    if basename.startswith("test_") or basename.startswith("conftest"):
        return True
    if any(part == "tests" for part in parts):
        return True
    return False


def _find_all_python_files() -> list[str]:
    """Find all Python files under dango/ and scripts/."""
    files = []
    for search_dir in ["dango", "scripts"]:
        if not os.path.isdir(search_dir):
            continue
        for root, dirs, filenames in os.walk(search_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(".py"):
                    files.append(os.path.join(root, fn))
    return files


def check_file(path: str) -> list[str]:
    """Check a single file for missing docstrings. Returns list of violations."""
    violations = []
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=path)
    except (SyntaxError, UnicodeDecodeError):
        return []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # Skip private/protected names
        if node.name.startswith("_"):
            continue
        docstring = ast.get_docstring(node)
        if docstring is None:
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            violations.append(f"  line {node.lineno}: {kind} '{node.name}' missing docstring")

    return violations


def main() -> int:
    """Check that public functions and classes have docstrings."""
    parser = argparse.ArgumentParser(description="Check public function/class docstrings")
    parser.add_argument("files", nargs="*", help="Files to check (pre-commit mode)")
    parser.add_argument("--all", action="store_true", help="Audit all Python files (CI mode)")
    args = parser.parse_args()

    if args.all:
        files = _find_all_python_files()
    elif args.files:
        files = args.files
    else:
        parser.error("Provide file paths or use --all")
        return 1

    total_violations = 0
    files_with_violations = 0

    for path in files:
        if _should_skip(path):
            continue
        if not os.path.isfile(path):
            continue

        violations = check_file(path)
        if violations:
            files_with_violations += 1
            total_violations += len(violations)
            print(f"{path}:")
            for v in violations:
                print(v)

    if total_violations > 0:
        print(f"\n{total_violations} missing docstring(s) in {files_with_violations} file(s).")
        return 1
    else:
        print("All public functions/classes have docstrings.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
