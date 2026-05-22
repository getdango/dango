#!/usr/bin/env python3
"""scripts/validate_claude_md.py

Validate CLAUDE.md files against the required template structure.
"""

import argparse
import re
import sys
from pathlib import Path

REQUIRED_SECTIONS = [
    "Purpose",
    "Files",
    "Common Tasks",
    "Dependencies",
    "Testing",
    "Don't Modify",
]

# Repo-root CLAUDE.md has a different structure (managed by DOC-000)
SKIP_PATHS = {"CLAUDE.md"}


def strip_code_fences(content: str) -> str:
    """Remove fenced code blocks so we don't match headings inside them."""
    return re.sub(r"^```.*?^```", "", content, flags=re.MULTILINE | re.DOTALL)


def find_h2_sections(content: str) -> list[str]:
    """Extract all H2 section titles from markdown content (outside code fences)."""
    return re.findall(r"^## (.+)$", strip_code_fences(content), re.MULTILINE)


def get_section_content(content: str, section_name: str) -> str:
    """Extract content under a specific H2 section."""
    pattern = rf"^## {re.escape(section_name)}\s*\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, content, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def has_markdown_table(text: str) -> bool:
    """Check if text contains a markdown table (line with | separators)."""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3:
            return True
    return False


def validate_file(file_path: Path) -> list[str]:
    """
    Validate a single CLAUDE.md file.

    Args:
        file_path: Path to the CLAUDE.md file

    Returns:
        List of error messages (empty if valid)
    """
    errors: list[str] = []

    if not file_path.exists():
        return [f"File not found: {file_path}"]

    raw_content = file_path.read_text(encoding="utf-8")
    content = strip_code_fences(raw_content)
    sections = find_h2_sections(raw_content)

    # Check all required sections exist
    for required in REQUIRED_SECTIONS:
        if required not in sections:
            errors.append(f"Missing required section: ## {required}")

    # Check Purpose is non-empty
    purpose_content = get_section_content(content, "Purpose")
    if "Purpose" in sections and not purpose_content:
        errors.append("## Purpose section is empty")

    # Check Files has a table
    files_content = get_section_content(content, "Files")
    if "Files" in sections and not has_markdown_table(files_content):
        errors.append("## Files section must contain a markdown table")

    return errors


def find_claude_md_files(root: Path) -> list[Path]:
    """Find all CLAUDE.md files under root, excluding repo-root."""
    results = []
    for path in sorted(root.rglob("CLAUDE.md")):
        # Skip repo-root CLAUDE.md and anything in hidden dirs or __pycache__
        rel = path.relative_to(root)
        parts = rel.parts
        if str(rel) in SKIP_PATHS:
            continue
        if any(p.startswith(".") or p == "__pycache__" for p in parts):
            continue
        results.append(path)
    return results


def main() -> int:
    """Validate CLAUDE.md files against the required template structure."""
    parser = argparse.ArgumentParser(
        description="Validate CLAUDE.md files against the required template structure."
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="CLAUDE.md file(s) to validate",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recursively find and validate all CLAUDE.md files under current directory",
    )
    args = parser.parse_args()

    if not args.files and not args.recursive:
        parser.error("Provide file paths or use --recursive")

    # Collect files
    files: list[Path] = []
    if args.recursive:
        root = Path.cwd()
        files = find_claude_md_files(root)
        if not files:
            print("No CLAUDE.md files found (excluding repo root).")
            return 0
    else:
        files = args.files

    # Validate — skip repo-root CLAUDE.md (different structure, managed by DOC-000)
    repo_root = Path.cwd()
    total_errors = 0
    validated = 0
    for file_path in files:
        try:
            rel = file_path.resolve().relative_to(repo_root)
            if str(rel) in SKIP_PATHS:
                continue
        except ValueError:
            pass
        validated += 1
        errors = validate_file(file_path)
        if errors:
            total_errors += len(errors)
            print(f"\n{file_path}:")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"{file_path}: OK")

    if total_errors > 0:
        print(f"\n{total_errors} error(s) in {validated} file(s).")
        return 1

    print(f"\nAll {validated} file(s) valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
