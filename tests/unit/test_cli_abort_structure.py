"""tests/unit/test_cli_abort_structure.py

No `raise click.Abort()` in dango/cli may sit inside a try whose broad `except Exception` handler
would swallow it (that prints a blank "Error:" and a second Abort).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CLI_DIR = Path(__file__).resolve().parents[2] / "dango" / "cli"


def _is_abort_raise(node: ast.AST) -> bool:
    if not isinstance(node, ast.Raise) or node.exc is None:
        return False
    exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    return (isinstance(exc, ast.Attribute) and exc.attr == "Abort") or (
        isinstance(exc, ast.Name) and exc.id == "Abort"
    )


def _handler_names(handler: ast.ExceptHandler) -> list[str]:
    if handler.type is None:
        return ["<bare>"]
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return [ast.unparse(t) for t in types]


def find_swallowed_aborts(tree: ast.AST) -> list[int]:
    """Line numbers of Abort raises caught by a broad handler of an enclosing try."""
    found: list[int] = []

    def walk(node: ast.AST, guards: list[ast.Try]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            guards = []
        if _is_abort_raise(node):
            for guard in guards:
                swallowed = False
                for handler in guard.handlers:
                    names = _handler_names(handler)
                    if any(n.endswith("Abort") for n in names):
                        break
                    if any(n in ("Exception", "BaseException", "<bare>") for n in names):
                        swallowed = True
                        break
                if swallowed:
                    found.append(node.lineno)  # type: ignore[attr-defined]
                    break
        if isinstance(node, ast.Try):
            for child in node.body:
                walk(child, [*guards, node])
            for handler in node.handlers:
                for child in handler.body:
                    walk(child, guards)
            for child in [*node.orelse, *node.finalbody]:
                walk(child, guards)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, guards)

    walk(tree, [])
    return found


def _scan(src: str) -> list[int]:
    return find_swallowed_aborts(ast.parse(src))


class TestDetectorPositiveControls:
    def test_flags_abort_in_broad_try(self) -> None:
        src = "try:\n    raise click.Abort()\nexcept Exception as e:\n    print(e)\n"
        assert _scan(src) == [2]

    def test_flags_bare_except_and_name_form(self) -> None:
        src = "try:\n    raise Abort\nexcept:\n    pass\n"
        assert _scan(src) == [2]

    def test_flags_abort_in_nested_if(self) -> None:
        src = "try:\n    if x:\n        raise click.Abort() from e\nexcept Exception:\n    pass\n"
        assert _scan(src) == [3]

    def test_clean_when_abort_reraised_first(self) -> None:
        src = (
            "try:\n    raise click.Abort()\nexcept click.Abort:\n    raise\n"
            "except Exception as e:\n    print(e)\n"
        )
        assert _scan(src) == []

    def test_clean_when_raised_in_handler(self) -> None:
        src = "try:\n    pass\nexcept Exception as e:\n    raise click.Abort() from e\n"
        assert _scan(src) == []

    def test_clean_when_only_narrow_handler(self) -> None:
        src = "try:\n    raise click.Abort()\nexcept KeyboardInterrupt:\n    pass\n"
        assert _scan(src) == []


def test_cli_has_no_swallowed_aborts() -> None:
    files = sorted(CLI_DIR.rglob("*.py"))
    assert len(files) > 20
    offenders = [
        f"{p.relative_to(CLI_DIR)}:{line}"
        for p in files
        for line in _scan(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
