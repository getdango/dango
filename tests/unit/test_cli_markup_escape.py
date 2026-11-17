"""tests/unit/test_cli_markup_escape.py

Exception text printed into Rich markup from an except handler in dango/cli must be escaped.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Lexical check. A handler `except X as name:` that passes `name` or `str(name)` bare into an
# f-string argument of console.print / print_error / print_warning / print_success / print_info
# is flagged; `escape(str(name))` (or any other call wrapping it) is not. Messages built before
# the print (a variable holding str(e)) and `{e!r}` / format specs are not followed.

CLI_DIR = Path(__file__).resolve().parents[2] / "dango" / "cli"
_PRINTERS = {"print_error", "print_warning", "print_success", "print_info"}


def _is_exc_text(value: ast.AST, name: str) -> bool:
    if isinstance(value, ast.Name):
        return value.id == name
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "str"
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id == name
    )


def _is_printer(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in _PRINTERS
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "print"
        and isinstance(func.value, ast.Name)
        and func.value.id == "console"
    )


def find_unescaped(tree: ast.AST) -> list[int]:
    """Lines of printer calls that interpolate a handler's exception variable unescaped."""
    lines: set[int] = set()
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name:
            continue
        for stmt in handler.body:
            for call in ast.walk(stmt):
                if not isinstance(call, ast.Call) or not _is_printer(call):
                    continue
                for arg in call.args:
                    if isinstance(arg, ast.JoinedStr):
                        hit = any(
                            isinstance(v, ast.FormattedValue)
                            and v.conversion == -1
                            and v.format_spec is None
                            and _is_exc_text(v.value, handler.name)
                            for v in arg.values
                        )
                    else:
                        hit = _is_exc_text(arg, handler.name)
                    if hit:
                        lines.add(call.lineno)
    return sorted(lines)


def _scan(src: str) -> list[int]:
    return find_unescaped(ast.parse(src))


class TestDetectorControls:
    def test_flags_plain_interpolation(self) -> None:
        src = 'try:\n    x()\nexcept Exception as e:\n    console.print(f"[red]Error:[/red] {e}")\n'
        assert _scan(src) == [4]

    def test_flags_str_wrapper_and_other_name(self) -> None:
        src = 'try:\n    x()\nexcept OSError as exc:\n    print_error(f"bad {str(exc)}")\n'
        assert _scan(src) == [4]

    def test_flags_bare_argument(self) -> None:
        src = "try:\n    x()\nexcept Exception as e:\n    console.print(str(e))\n"
        assert _scan(src) == [4]

    def test_flags_multiline_call(self) -> None:
        src = 'try:\n    x()\nexcept Exception as e:\n    console.print(\n        f"a {e}"\n    )\n'
        assert _scan(src) == [4]

    def test_clean_when_escaped(self) -> None:
        src = (
            'try:\n    x()\nexcept Exception as e:\n    console.print(f"{escape(str(e))}")\n'
            '    print_error(f"{report.literal(e)}")\n'
        )
        assert _scan(src) == []

    def test_other_variables_are_not_flagged(self) -> None:
        src = 'try:\n    x()\nexcept Exception as e:\n    console.print(f"{name} {e.args}")\n'
        assert _scan(src) == []

    def test_non_printer_calls_are_not_flagged(self) -> None:
        src = 'try:\n    x()\nexcept Exception as e:\n    logger.error(f"{e}")\n'
        assert _scan(src) == []


def test_cli_handlers_escape_exception_text() -> None:
    files = sorted(CLI_DIR.rglob("*.py"))
    assert len(files) > 20
    offenders = [
        f"{p.relative_to(CLI_DIR)}:{line}"
        for p in files
        for line in find_unescaped(ast.parse(p.read_text(encoding="utf-8")))
    ]
    assert offenders == []
