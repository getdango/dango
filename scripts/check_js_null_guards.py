"""scripts/check_js_null_guards.py

Pre-commit check that scans JS files for getElementById()/querySelector()
calls whose results are used without null guards.

Detects two patterns:
  1. Direct chaining: document.getElementById('x').property
  2. Unguarded variable: assigned from getElementById(), used within 10 lines
     without an if (var)/if (!var) guard within 3 lines of assignment.

Exit codes: 0 = no unguarded lookups, 1 = violations found.
"""

from __future__ import annotations

import os
import re
import sys

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JS_DIR = os.path.join("dango", "web", "static", "js")

# (function_name, element_id) tuples for page-specific functions where the
# element is guaranteed by the calling context.  Start empty — populate as
# needed.
ALLOWLIST: list[tuple[str, str]] = []

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Pattern 1: direct chaining — result used immediately without assignment
#   document.getElementById('x').classList
#   document.querySelector('.x').textContent
DIRECT_CHAIN_RE = re.compile(r"document\.(getElementById|querySelector)\s*\([^)]+\)\s*\.")

# Pattern 2: variable assignment from getElementById/querySelector
ASSIGN_RE = re.compile(
    r"(?:const|let|var)\s+(\w+)\s*=\s*document\.(getElementById|querySelector)\s*\("
)

# Safe: querySelectorAll (returns NodeList, never null)
QSA_RE = re.compile(r"querySelectorAll")

# Safe: optional chaining — ?.
OPTIONAL_CHAIN_RE = re.compile(r"document\.(getElementById|querySelector)\s*\([^)]+\)\s*\?\.")

# Safe: boolean coercion — !!document.getElementById(...)
BOOL_COERCE_RE = re.compile(r"!!\s*document\.(getElementById|querySelector)\s*\(")

# Safe: inside an if-condition — if (document.getElementById(...))
IF_WRAP_RE = re.compile(r"if\s*\(\s*!?\s*document\.(getElementById|querySelector)\s*\(")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_repo_root() -> str:
    """Walk upward from the script location looking for pyproject.toml."""
    path = os.path.dirname(os.path.abspath(__file__))
    for _ in range(10):
        if os.path.isfile(os.path.join(path, "pyproject.toml")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    # Fallback: assume script is in scripts/ under repo root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_js_files(repo_root: str) -> list[str]:
    """Return all .js files under the JS_DIR relative to repo_root."""
    js_dir = os.path.join(repo_root, JS_DIR)
    if not os.path.isdir(js_dir):
        return []
    return sorted(os.path.join(js_dir, f) for f in os.listdir(js_dir) if f.endswith(".js"))


def _get_enclosing_function(lines: list[str], line_idx: int) -> str | None:
    """Find the nearest enclosing function name above line_idx."""
    func_re = re.compile(r"(?:async\s+)?function\s+(\w+)\s*\(")
    for i in range(line_idx - 1, -1, -1):
        m = func_re.search(lines[i])
        if m:
            return m.group(1)
    return None


def _extract_element_id(line: str) -> str | None:
    """Extract the element ID string from a getElementById('id') call."""
    m = re.search(r"getElementById\s*\(\s*['\"]([^'\"]+)['\"]", line)
    if m:
        return m.group(1)
    return None


def _is_guard_present(lines: list[str], var_name: str, assign_idx: int) -> bool:
    """Check if a null guard for var_name exists within 3 lines after assignment."""
    guard_re = re.compile(
        rf"if\s*\(\s*!?\s*{re.escape(var_name)}\b"
        rf"|{re.escape(var_name)}\s*&&"
        rf"|{re.escape(var_name)}\s*\?"
        rf"|!{re.escape(var_name)}\b"
    )
    end = min(assign_idx + 4, len(lines))  # 3 lines after = indices +1, +2, +3
    for i in range(assign_idx + 1, end):
        if guard_re.search(lines[i]):
            return True
    return False


def _is_used_unguarded(
    lines: list[str],
    var_name: str,
    assign_idx: int,
) -> int | None:
    """Check if var_name is used (property access) within 10 lines without guard.

    Returns the line index of first unguarded use, or None if safe.
    """
    use_re = re.compile(rf"\b{re.escape(var_name)}\s*\.")
    end = min(assign_idx + 11, len(lines))  # 10 lines after
    for i in range(assign_idx + 1, end):
        if use_re.search(lines[i]):
            # Found a use — was there a guard before this use?
            if _is_guard_present(lines, var_name, assign_idx):
                return None
            return i
    return None


# ---------------------------------------------------------------------------
# File checker
# ---------------------------------------------------------------------------


def _check_file(filepath: str) -> list[tuple[int, str]]:
    """Check a single JS file. Returns list of (line_number, message)."""
    with open(filepath, encoding="utf-8") as f:
        lines = f.readlines()

    violations: list[tuple[int, str]] = []

    for idx, line in enumerate(lines):
        stripped = line.strip()

        # Skip comments
        if stripped.startswith("//") or stripped.startswith("*"):
            continue

        # --- Pattern 1: Direct chaining ---
        if DIRECT_CHAIN_RE.search(line):
            # Skip safe patterns
            if QSA_RE.search(line):
                continue
            if OPTIONAL_CHAIN_RE.search(line):
                continue
            if BOOL_COERCE_RE.search(line):
                continue
            if IF_WRAP_RE.search(line):
                continue

            # Check allowlist
            element_id = _extract_element_id(line)
            if element_id:
                func_name = _get_enclosing_function(lines, idx)
                if func_name and (func_name, element_id) in ALLOWLIST:
                    continue

            violations.append(
                (
                    idx + 1,
                    f"Direct chaining without null check: {stripped[:100]}",
                )
            )

        # --- Pattern 2: Unguarded variable assignment ---
        m = ASSIGN_RE.search(line)
        if m:
            var_name = m.group(1)
            method = m.group(2)

            # Skip querySelectorAll
            if QSA_RE.search(line):
                continue

            # Check allowlist
            element_id = _extract_element_id(line)
            if element_id:
                func_name = _get_enclosing_function(lines, idx)
                if func_name and (func_name, element_id) in ALLOWLIST:
                    continue

            use_idx = _is_used_unguarded(lines, var_name, idx)
            if use_idx is not None:
                violations.append(
                    (
                        idx + 1,
                        f"'{var_name}' from {method}() used at line {use_idx + 1} "
                        f"without null guard",
                    )
                )

    return violations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Scan JS files and report unguarded DOM lookups."""
    repo_root = _find_repo_root()
    js_files = _find_js_files(repo_root)

    if not js_files:
        print(f"No .js files found in {JS_DIR}/")
        return 0

    all_violations: list[tuple[str, int, str]] = []

    for filepath in js_files:
        violations = _check_file(filepath)
        rel = os.path.relpath(filepath, repo_root)
        for line_num, msg in violations:
            all_violations.append((rel, line_num, msg))

    if not all_violations:
        print(f"JS null guard check: {len(js_files)} files scanned, no issues found.")
        return 0

    print(f"JS null guard check: {len(all_violations)} unguarded DOM lookup(s) found:\n")
    for rel_path, line_num, msg in all_violations:
        print(f"  {rel_path}:{line_num}: {msg}")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
