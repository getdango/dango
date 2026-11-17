"""tests/unit/test_portability_static_rule.py

Static (AST) rule: local dango code must not use Unix-only modules or os/signal attributes unguarded.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "dango"

# Linux-server-only code (runs root-run on cloud hosts); exempt from the rule.
SERVER_ONLY = ("dango/platform/cloud/", "dango/runtime/")

UNIX_ONLY_MODULES = frozenset({"fcntl", "pwd", "grp", "resource", "termios"})
UNIX_ONLY_ATTRS = {
    "os": frozenset(
        {
            "fchmod",
            "fchown",
            "chown",
            "killpg",
            "getpgid",
            "getpgrp",
            "setsid",
            "geteuid",
            "getuid",
            "getgid",
            "O_NOFOLLOW",
            "O_NONBLOCK",
            "O_DIRECTORY",
        }
    ),
    "signal": frozenset({"SIGHUP", "SIGALRM", "SIGKILL", "SIGUSR1", "alarm"}),
}

# Sites owned by WP-11b (process groups / signals / detach semantics). Shrink-only: an entry that no
# longer matches a real site fails ``test_known_unfixed_entries_are_still_real``. Key = (file, name);
# imports are named ``import <module>``.
KNOWN_UNFIXED = {
    ("dango/notebooks/marimo_process.py", "killpg"),  # WP-11b
    ("dango/notebooks/marimo_process.py", "getpgid"),  # WP-11b
    ("dango/notebooks/marimo_process.py", "getpgrp"),  # WP-11b
    ("dango/notebooks/marimo_process.py", "SIGKILL"),  # WP-11b
    ("dango/platform/run_as.py", "killpg"),  # WP-11b
    ("dango/platform/run_as.py", "import pwd"),  # WP-11b
    ("dango/platform/script_launch.py", "killpg"),  # WP-11b
    ("dango/ingestion/dlt_runner.py", "SIGALRM"),  # WP-11b
    ("dango/ingestion/dlt_runner.py", "alarm"),  # WP-11b
    ("dango/web/helpers.py", "alarm"),  # WP-11b
}


def _is_sys_platform(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "platform"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _is_os_name(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "name"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _const(node: ast.expr) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _platform_kind(test: ast.expr) -> str | None:
    """Return "win" when ``test`` is true only on Windows, "posix" when it is false on Windows."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _platform_kind(test.operand)
        return {"win": "posix", "posix": "win"}.get(inner or "")
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Attribute)
        and test.func.attr == "startswith"
        and _is_sys_platform(test.func.value)
        and test.args
        and _const(test.args[0]) == "win"
    ):
        return "win"
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1:
        left, op, right = test.left, test.ops[0], test.comparators[0]
        value = _const(right)
        equal = isinstance(op, ast.Eq)
        if not (equal or isinstance(op, ast.NotEq)):
            return None
        if _is_sys_platform(left) and value == "win32":
            return "win" if equal else "posix"
        if _is_os_name(left) and value == "nt":
            return "win" if equal else "posix"
        if _is_os_name(left) and value == "posix":
            return "posix" if equal else "win"
    return None


def _hasattr_names(test: ast.expr) -> set[tuple[str, str]]:
    """(module, attribute) pairs a ``hasattr(os, "x")`` test (possibly inside ``and``) proves present."""
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return set().union(*(_hasattr_names(v) for v in test.values))
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Name)
        and test.func.id == "hasattr"
        and len(test.args) == 2
        and isinstance(test.args[0], ast.Name)
        and isinstance(_const(test.args[1]), str)
    ):
        return {(test.args[0].id, str(_const(test.args[1])))}
    return set()


def _ends_in_exit(body: list[ast.stmt]) -> bool:
    return bool(body) and isinstance(body[-1], (ast.Return, ast.Raise, ast.Continue, ast.Break))


def _catches(handlers: list[ast.ExceptHandler], names: set[str]) -> bool:
    for handler in handlers:
        types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        if any(isinstance(t, ast.Name) and t.id in names for t in types):
            return True
    return False


class _Checker:
    """Collect (line, name) of unguarded Unix-only imports and os/signal attributes.

    Accepted guard forms (anything else is a finding):
      1. import inside ``try`` whose handlers catch ImportError/ModuleNotFoundError;
      2. the branch of ``if sys.platform == "win32"`` / ``!= "win32"`` / ``.startswith("win")`` /
         ``os.name == "nt"|"posix"`` (and ``not`` of those) that is not taken on Windows, or the code after
         such an ``if`` whose Windows branch ends in return/raise/continue/break;
      3. the body of ``if hasattr(os, "x"):`` (``and`` allowed) or an ``x if hasattr(os, "x") else y``
         expression, for that attribute only.
    ``getattr(os, "x", default)`` is never flagged because the attribute is not an Attribute node; the
    two-argument ``getattr(os, "x")`` (no default) is flagged.
    """

    def __init__(self) -> None:
        self.found: list[tuple[int, str]] = []

    def block(self, stmts: list[ast.stmt], safe: bool, present: frozenset[tuple[str, str]]) -> None:
        for stmt in stmts:
            self.stmt(stmt, safe, present)
            if isinstance(stmt, ast.If) and _ends_in_exit(stmt.body):
                if _platform_kind(stmt.test) == "win":
                    safe = True

    def stmt(self, node: ast.stmt, safe: bool, present: frozenset[tuple[str, str]]) -> None:
        if isinstance(node, ast.If):
            kind = _platform_kind(node.test)
            self.expr(node.test, safe, present)
            body_present = present | _hasattr_names(node.test)
            self.block(node.body, safe or kind == "posix", body_present)
            self.block(node.orelse, safe or kind == "win", present)
        elif isinstance(node, ast.Try):
            catches_import = _catches(node.handlers, {"ImportError", "ModuleNotFoundError"})
            self.block(node.body, safe or catches_import, present)
            for handler in node.handlers:
                self.block(handler.body, safe, present)
            self.block(node.orelse, safe, present)
            self.block(node.finalbody, safe, present)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            self.imports(node, safe)
        else:
            for _field, value in ast.iter_fields(node):
                if isinstance(value, list):
                    if value and isinstance(value[0], ast.stmt):
                        self.block(value, safe, present)
                    else:
                        for item in value:
                            self.walk(item, safe, present)
                else:
                    self.walk(value, safe, present)

    def walk(self, node: object, safe: bool, present: frozenset[tuple[str, str]]) -> None:
        if isinstance(node, ast.stmt):
            self.stmt(node, safe, present)
        elif isinstance(node, ast.expr):
            self.expr(node, safe, present)
        elif isinstance(node, ast.AST):
            for child in ast.iter_child_nodes(node):
                self.walk(child, safe, present)

    def imports(self, node: ast.Import | ast.ImportFrom, safe: bool) -> None:
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        else:
            roots = [(node.module or "").split(".")[0]] if node.level == 0 else []
            if node.module in UNIX_ONLY_ATTRS and not safe:
                for alias in node.names:
                    if alias.name in UNIX_ONLY_ATTRS[node.module]:
                        self.found.append((node.lineno, alias.name))
        for root in roots:
            if root in UNIX_ONLY_MODULES and not safe:
                self.found.append((node.lineno, f"import {root}"))

    def expr(self, node: ast.expr, safe: bool, present: frozenset[tuple[str, str]]) -> None:
        if isinstance(node, ast.IfExp):
            self.expr(node.test, safe, present)
            self.expr(node.body, safe, present | _hasattr_names(node.test))
            self.expr(node.orelse, safe, present)
            return
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in UNIX_ONLY_ATTRS
            and _const(node.args[1]) in UNIX_ONLY_ATTRS[node.args[0].id]
        ):
            self.found.append((node.lineno, str(_const(node.args[1]))))
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in UNIX_ONLY_ATTRS
            and node.attr in UNIX_ONLY_ATTRS[node.value.id]
            and not safe
            and (node.value.id, node.attr) not in present
        ):
            self.found.append((node.lineno, node.attr))
        for child in ast.iter_child_nodes(node):
            self.walk(child, safe, present)


def find_unguarded(source: str) -> list[tuple[int, str]]:
    """Return (line, name) for every unguarded Unix-only import or attribute in ``source``."""
    checker = _Checker()
    checker.block(ast.parse(source).body, False, frozenset())
    return checker.found


def _local_files() -> list[Path]:
    files = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not rel.startswith(SERVER_ONLY):
            files.append(path)
    return files


def _scan_tree() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in _local_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for _line, name in find_unguarded(path.read_text(encoding="utf-8")):
            found.add((rel, name))
    return found


def test_no_new_unguarded_unix_only_use_in_local_code() -> None:
    new = sorted(_scan_tree() - KNOWN_UNFIXED)
    assert not new, f"unguarded Unix-only use in local code (see docstring of _Checker): {new}"


def test_known_unfixed_entries_are_still_real() -> None:
    stale = sorted(KNOWN_UNFIXED - _scan_tree())
    assert not stale, f"KNOWN_UNFIXED entries that no longer match a site (delete them): {stale}"


def test_server_only_packages_exist() -> None:
    for prefix in SERVER_ONLY:
        assert (REPO_ROOT / prefix).is_dir(), prefix


# Positive controls: the checker itself, on synthetic sources.


@pytest.mark.parametrize(
    ("source", "names"),
    [
        ("import fcntl\n", ["import fcntl"]),
        ("def f():\n    import pwd\n", ["import pwd"]),
        ("import os\nos.fchmod(1, 0o600)\n", ["fchmod"]),
        ("import os\nfd = os.O_RDONLY | os.O_NOFOLLOW\n", ["O_NOFOLLOW"]),
        ("import signal\nsignal.signal(signal.SIGALRM, h)\n", ["SIGALRM"]),
        ("import signal\nsignal.alarm(3)\n", ["alarm"]),
        ("from os import killpg\n", ["killpg"]),
        ("import os\nx = getattr(os, 'killpg')\n", ["killpg"]),
        ("import sys, os\nif True:\n    os.killpg(1, 9)\n", ["killpg"]),
        ("import sys, os\nif sys.platform == 'win32':\n    os.killpg(1, 9)\n", ["killpg"]),
        (
            "import sys, os\nif sys.platform != 'win32':\n    pass\nelse:\n    os.getuid()\n",
            ["getuid"],
        ),
        ("import os\nif hasattr(os, 'getuid'):\n    pass\nelse:\n    os.getuid()\n", ["getuid"]),
        ("import os\nif hasattr(os, 'getuid'):\n    os.getgid()\n", ["getgid"]),
        ("try:\n    import fcntl\nexcept ValueError:\n    pass\n", ["import fcntl"]),
    ],
)
def test_checker_detects(source: str, names: list[str]) -> None:
    assert [name for _line, name in find_unguarded(source)] == names


@pytest.mark.parametrize(
    "source",
    [
        "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n",
        "try:\n    import pwd\nexcept (ImportError, OSError):\n    pwd = None\n",
        "import sys\nif sys.platform != 'win32':\n    import fcntl\n",
        "import sys\nif sys.platform == 'win32':\n    import msvcrt\nelse:\n    import fcntl\n",
        "import sys\nif not sys.platform.startswith('win'):\n    import fcntl\n",
        "import os\nif os.name == 'posix':\n    import pwd\n",
        "import os\nif os.name != 'nt':\n    os.geteuid()\nelse:\n    pass\n",
        "import os\nfd = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)\n",
        "import signal\nsigs = [signal.SIGINT, getattr(signal, 'SIGHUP', None)]\n",
        "import os\nif hasattr(os, 'fchmod'):\n    os.fchmod(1, 0o600)\n",
        "import os\nx = os.getuid() if hasattr(os, 'getuid') else 0\n",
        "import os\nif hasattr(os, 'getuid') and True:\n    os.getuid()\n",
        "import sys\ndef f():\n    if sys.platform == 'win32':\n        return 1\n    import fcntl\n",
        "import os\nos.O_RDONLY\n",
        "from os import O_RDONLY\n",
    ],
)
def test_checker_accepts(source: str) -> None:
    assert find_unguarded(source) == []
