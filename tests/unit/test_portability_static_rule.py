"""tests/unit/test_portability_static_rule.py

Static (AST) rule: local dango code must not use Unix-only modules or os/signal attributes unguarded.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "dango"

# Linux-server-only code (runs root-run on cloud hosts); exempt from the rule.
SERVER_ONLY = ("dango/platform/cloud/", "dango/runtime/")

# Each name is documented by CPython as Unix-only (absent on Windows): the ``posix`` module (Windows has
# ``nt``), os.setpgrp/fork/forkpty/openpty/mkfifo/nice/wait/WNOHANG, signal.SIGQUIT/SIGUSR2/SIGCHLD/SIGPIPE/
# setitimer/getitimer. ``os.waitpid`` and ``os.getppid`` exist on Windows and are deliberately not listed.
UNIX_ONLY_MODULES = frozenset({"fcntl", "pwd", "grp", "resource", "termios", "posix"})
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
            "setpgrp",
            "fork",
            "forkpty",
            "openpty",
            "mkfifo",
            "nice",
            "wait",
            "WNOHANG",
        }
    ),
    "signal": frozenset(
        {
            "SIGHUP",
            "SIGALRM",
            "SIGKILL",
            "SIGUSR1",
            "alarm",
            "SIGQUIT",
            "SIGUSR2",
            "SIGCHLD",
            "SIGPIPE",
            "setitimer",
            "getitimer",
        }
    ),
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


@dataclass(frozen=True)
class _Ctx:
    """Guard state: ``safe`` = platform-guarded (imports and attributes), ``imports_ok`` = inside a
    ``try`` whose handlers catch ImportError (imports only), ``present`` = names proven by ``hasattr``."""

    safe: bool = False
    imports_ok: bool = False
    present: frozenset[tuple[str, str]] = frozenset()


class _Checker:
    """Collect (line, name) of unguarded Unix-only imports and os/signal attributes.

    Accepted guard forms (anything else is a finding):
      1. import inside ``try`` whose handlers catch ImportError/ModuleNotFoundError (imports ONLY: a
         missing attribute raises AttributeError, which that handler does not catch);
      2. the branch of ``if sys.platform == "win32"`` / ``!= "win32"`` / ``.startswith("win")`` /
         ``os.name == "nt"|"posix"`` (and ``not`` of those) that is not taken on Windows, or the code after
         such an ``if`` whose Windows branch ends in return/raise/continue/break;
      3. the body of ``if hasattr(os, "x"):`` (``and`` allowed) or an ``x if hasattr(os, "x") else y``
         expression, for that attribute only.
    ``getattr(os, "x", default)`` is never flagged because the attribute is not an Attribute node; the
    two-argument ``getattr(os, "x")`` (no default) is flagged. ``import os as _os`` / ``import signal as s``
    aliases are resolved; ``from os import fchmod [as f]`` is flagged at the import.
    """

    def __init__(self, aliases: dict[str, str]) -> None:
        self.found: list[tuple[int, str]] = []
        self.aliases = aliases

    def block(self, stmts: list[ast.stmt], ctx: _Ctx) -> None:
        for stmt in stmts:
            self.stmt(stmt, ctx)
            if (
                isinstance(stmt, ast.If)
                and _ends_in_exit(stmt.body)
                and _platform_kind(stmt.test) == "win"
            ):
                ctx = replace(ctx, safe=True)

    def stmt(self, node: ast.stmt, ctx: _Ctx) -> None:
        if isinstance(node, ast.If):
            kind = _platform_kind(node.test)
            self.expr(node.test, ctx)
            body_ctx = replace(
                ctx,
                safe=ctx.safe or kind == "posix",
                present=ctx.present | _hasattr_names(node.test),
            )
            self.block(node.body, body_ctx)
            self.block(node.orelse, replace(ctx, safe=ctx.safe or kind == "win"))
        elif isinstance(node, ast.Try):
            catches_import = _catches(node.handlers, {"ImportError", "ModuleNotFoundError"})
            self.block(node.body, replace(ctx, imports_ok=ctx.imports_ok or catches_import))
            for handler in node.handlers:
                self.block(handler.body, ctx)
            self.block(node.orelse, ctx)
            self.block(node.finalbody, ctx)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            self.imports(node, ctx)
        else:
            for _field, value in ast.iter_fields(node):
                if isinstance(value, list):
                    if value and isinstance(value[0], ast.stmt):
                        self.block(value, ctx)
                    else:
                        for item in value:
                            self.walk(item, ctx)
                else:
                    self.walk(value, ctx)

    def walk(self, node: object, ctx: _Ctx) -> None:
        if isinstance(node, ast.stmt):
            self.stmt(node, ctx)
        elif isinstance(node, ast.expr):
            self.expr(node, ctx)
        elif isinstance(node, ast.AST):
            for child in ast.iter_child_nodes(node):
                self.walk(child, ctx)

    def imports(self, node: ast.Import | ast.ImportFrom, ctx: _Ctx) -> None:
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        else:
            roots = [(node.module or "").split(".")[0]] if node.level == 0 else []
            if node.module in UNIX_ONLY_ATTRS and not (ctx.safe or ctx.imports_ok):
                for alias in node.names:
                    if alias.name in UNIX_ONLY_ATTRS[node.module]:
                        self.found.append((node.lineno, alias.name))
        for root in roots:
            if root in UNIX_ONLY_MODULES and not (ctx.safe or ctx.imports_ok):
                self.found.append((node.lineno, f"import {root}"))

    def expr(self, node: ast.expr, ctx: _Ctx) -> None:
        if isinstance(node, ast.IfExp):
            self.expr(node.test, ctx)
            self.expr(node.body, replace(ctx, present=ctx.present | _hasattr_names(node.test)))
            self.expr(node.orelse, ctx)
            return
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Name)
            and self.aliases.get(node.args[0].id) in UNIX_ONLY_ATTRS
            and _const(node.args[1]) in UNIX_ONLY_ATTRS[self.aliases[node.args[0].id]]
        ):
            self.found.append((node.lineno, str(_const(node.args[1]))))
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and self.aliases.get(node.value.id) in UNIX_ONLY_ATTRS
            and node.attr in UNIX_ONLY_ATTRS[self.aliases[node.value.id]]
            and not ctx.safe
            and (node.value.id, node.attr) not in ctx.present
        ):
            self.found.append((node.lineno, node.attr))
        for child in ast.iter_child_nodes(node):
            self.walk(child, ctx)


def find_unguarded(source: str) -> list[tuple[int, str]]:
    """Return (line, name) for every unguarded Unix-only import or attribute in ``source``."""
    tree = ast.parse(source)
    aliases = {module: module for module in UNIX_ONLY_ATTRS}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name in UNIX_ONLY_ATTRS:
                    aliases[alias.asname] = alias.name
    checker = _Checker(aliases)
    checker.block(tree.body, _Ctx())
    return checker.found


def _is_server_only(rel: str) -> bool:
    return rel.startswith(SERVER_ONLY)


def _local_files() -> list[Path]:
    files = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not _is_server_only(rel):
            files.append(path)
    return files


def _scan_tree() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in _local_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for _line, name in find_unguarded(path.read_text(encoding="utf-8")):
            found.add((rel, name))
    return found


def test_no_unguarded_unix_only_use_in_local_code() -> None:
    found = sorted(_scan_tree())
    assert not found, f"unguarded Unix-only use in local code (see docstring of _Checker): {found}"


def test_server_only_matching_is_exact_on_directory_boundary() -> None:
    assert _is_server_only("dango/platform/cloud/x.py")
    assert _is_server_only("dango/runtime/isolated_run.py")
    assert not _is_server_only("dango/platform/cloud_x.py")
    assert not _is_server_only("dango/platform/cloudy/x.py")
    assert not _is_server_only("dango/runtimex.py")


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
        ("import os as _os\nx = getattr(_os, 'killpg')\n", ["killpg"]),
        # an ImportError handler body and the else branch do not inherit the import guard
        ("try:\n    import json\nexcept ImportError:\n    import fcntl\n", ["import fcntl"]),
        (
            "try:\n    import json\nexcept ImportError:\n    pass\nelse:\n    import fcntl\n",
            ["import fcntl"],
        ),
        # an ImportError handler does not catch the AttributeError a missing attribute raises
        (
            "import os\ntry:\n    import json\n    os.fchmod(1, 0o600)\nexcept ImportError:\n    pass\n",
            ["fchmod"],
        ),
        (
            "import signal\ntry:\n    signal.signal(signal.SIGHUP, h)\nexcept ImportError:\n    pass\n",
            ["SIGHUP"],
        ),
        # aliases and from-imports
        ("import os as _os\n_os.killpg(1, 9)\n", ["killpg"]),
        ("import signal as _sig\n_sig.SIGKILL\n", ["SIGKILL"]),
        ("from os import killpg as k\n", ["killpg"]),
        ("from signal import SIGKILL\n", ["SIGKILL"]),
        ("import posix\n", ["import posix"]),
        # more Unix-only names
        ("import os\nos.setpgrp()\n", ["setpgrp"]),
        ("import os\nos.fork()\n", ["fork"]),
        ("import os\nos.forkpty()\n", ["forkpty"]),
        ("import os\nos.openpty()\n", ["openpty"]),
        ("import os\nos.mkfifo('p')\n", ["mkfifo"]),
        ("import os\nos.nice(1)\n", ["nice"]),
        ("import os\nos.wait()\n", ["wait"]),
        ("import os\nos.WNOHANG\n", ["WNOHANG"]),
        ("import signal\nsignal.SIGQUIT\n", ["SIGQUIT"]),
        ("import signal\nsignal.SIGUSR2\n", ["SIGUSR2"]),
        ("import signal\nsignal.SIGCHLD\n", ["SIGCHLD"]),
        ("import signal\nsignal.SIGPIPE\n", ["SIGPIPE"]),
        ("import signal\nsignal.setitimer(0, 1)\n", ["setitimer"]),
        ("import signal\nsignal.getitimer(0)\n", ["getitimer"]),
        # an early exit only guards when it is taken on Windows and actually exits
        (
            "import sys, os\ndef f():\n    if sys.platform != 'win32':\n        return\n    os.killpg(1, 9)\n",
            ["killpg"],
        ),
        ("import sys, os\nif sys.platform == 'win32':\n    log()\nos.killpg(1, 9)\n", ["killpg"]),
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
        "import os as _os\nfd = getattr(_os, 'O_NOFOLLOW', 0)\n",
        "import os as _os\nif hasattr(_os, 'fchmod'):\n    _os.fchmod(1, 0)\n",
        "import os\nos.getcwd()\n",
        "import signal\nsigs = [signal.SIGINT, getattr(signal, 'SIGHUP', None)]\n",
        "import os\nif hasattr(os, 'fchmod'):\n    os.fchmod(1, 0o600)\n",
        "import os\nx = os.getuid() if hasattr(os, 'getuid') else 0\n",
        "import os\nif hasattr(os, 'getuid') and True:\n    os.getuid()\n",
        "import sys\ndef f():\n    if sys.platform == 'win32':\n        return 1\n    import fcntl\n",
        "import os\nos.O_RDONLY\n",
        "from os import O_RDONLY\n",
        # a from-import raises ImportError when the name is missing, so the handler catches it
        "try:\n    from os import killpg\nexcept ImportError:\n    killpg = None\n",
    ],
)
def test_checker_accepts(source: str) -> None:
    assert find_unguarded(source) == []
