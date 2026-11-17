"""tests/unit/test_cli_abort_structure.py

No click.Abort (raised directly or by a CLI helper) may be swallowed by a broad except in dango/cli.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Limits of this check (lexical): helpers are the undecorated MODULE-LEVEL functions in dango/cli
# that raise click.Abort directly or call a click source below, plus the module-level functions that
# call one of those by bare name (one level). Bare-name calls match helper names only; attribute
# calls match only the dotted names in ABORT_SOURCES (never `x.run(...)`, so `subprocess.run`
# does not collide with a helper named `run`). Methods, callbacks, callees outside that set, and a
# nested function defined and called inside the try are not followed. `except (click.Abort,
# Exception)` counts as handled without checking its body, and a broad handler that re-raises Abort
# through `isinstance` is flagged (false positive). Broad = Exception, RuntimeError (the base of
# click.Abort and click.exceptions.Exit), BaseException or a bare except. click.confirm/prompt raise
# Abort on Ctrl+C/EOF; ctx.exit raises click's Exit.

CLI_DIR = Path(__file__).resolve().parents[2] / "dango" / "cli"
_BROAD = ("Exception", "RuntimeError", "BaseException", "<bare>")
ABORT_SOURCES = frozenset({"click.confirm", "click.prompt", "ctx.exit"})

# (file relative to dango/cli, enclosing function) -> why a swallowed Abort there is intended.
ALLOWED: dict[tuple[str, str], str] = {
    ("init.py", "_ask_telemetry_consent"): "an aborted consent prompt means no answer",
    (
        "init.py",
        "_setup_auth",
    ): "interactive auth-setup prompts; cancel handling is reviewed separately",
}


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


def _is_source_call(node: ast.AST) -> bool:
    """A call to one of ABORT_SOURCES (full dotted name on a plain name)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and f"{node.func.value.id}.{node.func.attr}" in ABORT_SOURCES
    )


def _bare_call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _swallows(guard: ast.Try) -> bool:
    for handler in guard.handlers:
        names = _handler_names(handler)
        if any(n.endswith("Abort") for n in names):
            return False
        if any(n in _BROAD for n in names):
            return True
    return False


def find_swallowed_aborts(
    tree: ast.AST, helpers: frozenset[str] = frozenset()
) -> list[tuple[int, str]]:
    """(line, enclosing function) of Abort raises, source calls or helper calls under a broad try."""
    found: list[tuple[int, str]] = []

    def walk(node: ast.AST, guards: list[ast.Try], func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            guards, func = [], node.name
        elif isinstance(node, ast.Lambda):
            guards = []
        is_hit = _is_abort_raise(node) or _is_source_call(node) or _bare_call_name(node) in helpers
        if is_hit and any(_swallows(g) for g in guards):
            found.append((getattr(node, "lineno", 0), func))
        if isinstance(node, ast.Try):
            for child in node.body:
                walk(child, [*guards, node], func)
            for handler in node.handlers:
                for child in handler.body:
                    walk(child, guards, func)
            for child in [*node.orelse, *node.finalbody]:
                walk(child, guards, func)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, guards, func)

    walk(tree, [], "<module>")
    return found


def derive_abort_helpers(trees: Iterable[ast.AST]) -> frozenset[str]:
    """Module-level undecorated functions that abort, plus their bare-name callers (one level)."""
    funcs = [
        n
        for t in trees
        if isinstance(t, ast.Module)
        for n in t.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.decorator_list
    ]
    direct = {
        f.name for f in funcs if any(_is_abort_raise(x) or _is_source_call(x) for x in ast.walk(f))
    }
    indirect = {
        f.name
        for f in funcs
        if f.name not in direct and any(_bare_call_name(x) in direct for x in ast.walk(f))
    }
    return frozenset(direct | indirect)


def _scan(src: str, helpers: frozenset[str] = frozenset()) -> list[int]:
    return [line for line, _ in find_swallowed_aborts(ast.parse(src), helpers)]


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

    def test_attribute_call_on_other_object_is_not_a_helper(self) -> None:
        src = "try:\n    subprocess.run(['x'])\nexcept Exception:\n    pass\n"
        assert _scan(src, frozenset({"run"})) == []

    def test_flags_click_confirm_and_prompt(self) -> None:
        for call in ("click.confirm('x')", "click.prompt('x')"):
            src = f"try:\n    {call}\nexcept Exception:\n    pass\n"
            assert _scan(src) == [2], call

    def test_flags_ctx_exit(self) -> None:
        src = "try:\n    ctx.exit(1)\nexcept Exception:\n    pass\n"
        assert _scan(src) == [2]

    def test_other_object_confirm_is_not_a_source(self) -> None:
        src = "try:\n    dialog.confirm('x')\nexcept Exception:\n    pass\n"
        assert _scan(src) == []

    def test_click_confirm_clean_with_abort_handler(self) -> None:
        src = (
            "try:\n    click.confirm('x')\nexcept click.Abort:\n    return False\n"
            "except Exception:\n    pass\n"
        )
        assert _scan(src) == []

    def test_reports_enclosing_function(self) -> None:
        src = "def f():\n    try:\n        click.prompt('x')\n    except Exception:\n        pass\n"
        assert find_swallowed_aborts(ast.parse(src)) == [(3, "f")]

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

    def test_methods_are_not_helpers(self) -> None:
        src = "class W:\n    def run(self):\n        raise click.Abort()\n"
        assert derive_abort_helpers([ast.parse(src)]) == frozenset()

    def test_function_calling_click_source_is_a_helper(self) -> None:
        src = "def ask():\n    return click.confirm('x')\ndef use():\n    ask()\n"
        assert derive_abort_helpers([ast.parse(src)]) == frozenset({"ask", "use"})


def _cli_trees() -> dict[Path, ast.AST]:
    files = sorted(CLI_DIR.rglob("*.py"))
    assert len(files) > 20
    return {p: ast.parse(p.read_text(encoding="utf-8")) for p in files}


def swallowed_in(trees: dict[Path, ast.AST]) -> list[str]:
    helpers = derive_abort_helpers(trees.values())
    return [
        f"{p.relative_to(CLI_DIR)}:{line} ({func})"
        for p, tree in trees.items()
        for line, func in find_swallowed_aborts(tree, helpers)
        if (p.relative_to(CLI_DIR).as_posix(), func) not in ALLOWED
    ]


def test_derived_helpers_include_known_ones() -> None:
    helpers = derive_abort_helpers(_cli_trees().values())
    assert {"require_project_context", "check_v01x_project", "safe_confirm"} <= helpers
    assert "run" not in helpers


def test_allowed_entries_still_swallow() -> None:
    """Every ALLOWED entry must still be a real swallow site (stale entries are errors)."""
    trees = _cli_trees()
    helpers = derive_abort_helpers(trees.values())
    seen = {
        (p.relative_to(CLI_DIR).as_posix(), func)
        for p, tree in trees.items()
        for _, func in find_swallowed_aborts(tree, helpers)
    }
    assert set(ALLOWED) <= seen


def test_cli_has_no_swallowed_aborts() -> None:
    assert swallowed_in(_cli_trees()) == []
