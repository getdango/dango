"""scripts/find_assertionless_tests.py

List test functions that contain no assertion of any kind: no ``assert`` statement,
no ``pytest.raises`` / ``pytest.warns`` / ``pytest.deprecated_call`` block, no ``pytest.fail``, and no call to
anything named ``assert*`` (``mock.assert_called_once``, ``self.assertEqual``, ``_assert_ok(...)``).
Only functions whose name starts with ``test`` are considered; fixtures and helpers are ignored.

Output: one ``path::name`` per line (``path::Class.name`` for methods).

Exit codes:
    0 — default, or ``--fail-on-hits`` with no hits
    1 — ``--fail-on-hits`` and at least one hit
"""

import argparse
import ast
import sys
from pathlib import Path

_RAISES_NAMES = {"raises", "warns", "deprecated_call"}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_pytest_fail(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "fail"
        and isinstance(func.value, ast.Name)
        and func.value.id == "pytest"
    )


def _has_assertion(func: ast.AST) -> bool:
    """Return True if the function body contains any assertion construct."""
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if "assert" in name.lower() or name in _RAISES_NAMES or _is_pytest_fail(node):
                return True
    return False


def _iter_tests(body: list[ast.stmt], prefix: str = ""):
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                yield prefix + node.name, node
        elif isinstance(node, ast.ClassDef):
            yield from _iter_tests(node.body, prefix + node.name + ".")


def find_in_file(path: Path) -> list[str]:
    """Return the qualified names of assertion-less test functions in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [name for name, node in _iter_tests(tree.body) if not _has_assertion(node)]


def find_in_paths(paths: list[Path]) -> list[str]:
    """Return ``path::name`` for every hit under the given files/directories."""
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.rglob("test_*.py")))
        else:
            files.append(p)
    hits: list[str] = []
    for f in files:
        hits.extend(f"{f}::{name}" for name in find_in_file(f))
    return hits


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["tests"], type=Path)
    parser.add_argument("--fail-on-hits", action="store_true")
    args = parser.parse_args(argv)
    hits = find_in_paths(args.paths)
    for hit in hits:
        print(hit)
    if args.fail_on_hits and hits:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
