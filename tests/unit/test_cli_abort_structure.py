"""tests/unit/test_cli_abort_structure.py

No click.Abort (raised directly or by a CLI helper) may be swallowed by a broad except in dango/cli.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Limits of this check (lexical, name-based): helpers are the undecorated functions in dango/cli
# that contain `raise click.Abort` directly, plus the undecorated functions that call one of those
# (one level). Calls are matched by the last name segment only (`utils.safe_confirm(...)` and
# `safe_confirm(...)` both match). Callees outside that set, callbacks, and a nested function that
# is defined and called inside the try are not followed. `except (click.Abort, Exception)` counts
# as handled without checking its body. Broad = Exception, RuntimeError (click.Abort's base),
# BaseException or a bare except.

CLI_DIR = Path(__file__).resolve().parents[2] / "dango" / "cli"
_BROAD = ("Exception", "RuntimeError", "BaseException", "<bare>")


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


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _swallows(guard: ast.Try) -> bool:
    for handler in guard.handlers:
        names = _handler_names(handler)
        if any(n.endswith("Abort") for n in names):
            return False
        if any(n in _BROAD for n in names):
            return True
    return False


def find_swallowed_aborts(tree: ast.AST, helpers: frozenset[str] = frozenset()) -> list[int]:
    """Lines of Abort raises, or calls to `helpers`, caught by a broad handler of an enclosing try."""
    found: list[int] = []

    def walk(node: ast.AST, guards: list[ast.Try]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            guards = []
        is_hit = _is_abort_raise(node) or (
            isinstance(node, ast.Call) and _call_name(node) in helpers
        )
        if is_hit and any(_swallows(g) for g in guards):
            found.append(getattr(node, "lineno", 0))
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


def derive_abort_helpers(trees: Iterable[ast.AST]) -> frozenset[str]:
    """Undecorated functions that raise click.Abort, plus callers of those (one level)."""
    funcs = [
        n
        for t in trees
        for n in ast.walk(t)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.decorator_list
    ]
    direct = {f.name for f in funcs if any(_is_abort_raise(x) for x in ast.walk(f))}
    indirect = {
        f.name
        for f in funcs
        if f.name not in direct
        and any(isinstance(x, ast.Call) and _call_name(x) in direct for x in ast.walk(f))
    }
    return frozenset(direct | indirect)


def _scan(src: str, helpers: frozenset[str] = frozenset()) -> list[int]:
    return find_swallowed_aborts(ast.parse(src), helpers)


class TestDetectorPositiveControls:
    def test_flags_abort_in_broad_try(self) -> None:
        src = "try:\n    raise click.Abort()\nexcept Exception as e:\n    print(e)\n"
        assert _scan(src) == [2]

    def test_flags_bare_except_and_name_form(self) -> None:
        src = "try:\n    raise Abort\nexcept:\n    pass\n"
        assert _scan(src) == [2]

    def test_flags_runtime_error_handler(self) -> None:
        src = "try:\n    raise click.Abort()\nexcept RuntimeError:\n    pass\n"
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

    def test_flags_helper_call_in_broad_try(self) -> None:
        src = (
            "try:\n    root = require_project_context(ctx)\nexcept Exception as e:\n    print(e)\n"
        )
        assert _scan(src, frozenset({"require_project_context"})) == [2]

    def test_flags_attribute_helper_call(self) -> None:
        src = "try:\n    utils.safe_confirm('x')\nexcept Exception:\n    pass\n"
        assert _scan(src, frozenset({"safe_confirm"})) == [2]

    def test_helper_call_clean_with_abort_passthrough(self) -> None:
        src = (
            "try:\n    require_project_context(ctx)\nexcept click.Abort:\n    raise\n"
            "except Exception as e:\n    print(e)\n"
        )
        assert _scan(src, frozenset({"require_project_context"})) == []

    def test_helper_call_outside_try_is_clean(self) -> None:
        src = "def f():\n    require_project_context(ctx)\n"
        assert _scan(src, frozenset({"require_project_context"})) == []

    def test_unlisted_call_is_not_flagged(self) -> None:
        src = "try:\n    other()\nexcept Exception:\n    pass\n"
        assert _scan(src, frozenset({"require_project_context"})) == []

    def test_derive_helpers_direct_and_one_level(self) -> None:
        src = (
            "def a():\n    raise click.Abort()\n"
            "def b():\n    a()\n"
            "def c():\n    b()\n"
            "@click.command()\ndef cmd():\n    raise click.Abort()\n"
        )
        assert derive_abort_helpers([ast.parse(src)]) == frozenset({"a", "b"})


def _cli_trees() -> dict[Path, ast.AST]:
    files = sorted(CLI_DIR.rglob("*.py"))
    assert len(files) > 20
    return {p: ast.parse(p.read_text(encoding="utf-8")) for p in files}


def test_derived_helpers_include_known_ones() -> None:
    helpers = derive_abort_helpers(_cli_trees().values())
    assert {"require_project_context", "check_v01x_project", "safe_confirm"} <= helpers


def test_cli_has_no_swallowed_aborts() -> None:
    trees = _cli_trees()
    helpers = derive_abort_helpers(trees.values())
    offenders = [
        f"{p.relative_to(CLI_DIR)}:{line}"
        for p, tree in trees.items()
        for line in find_swallowed_aborts(tree, helpers)
    ]
    assert offenders == []
