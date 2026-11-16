"""tests/unit/test_windows_import_simulation.py

Importing every local dango module must work when the POSIX-only stdlib modules are unavailable (as on Windows).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Modules that do not exist on Windows; the child process makes ``import <name>`` raise ModuleNotFoundError.
UNIX_ONLY_MODULES = ("fcntl", "pwd", "grp", "resource", "termios")

SERVER_ONLY_PACKAGES = ("dango.platform.cloud", "dango.runtime")  # Linux servers only (root-run)

# Dango modules that import a Unix-only module behind an ``if sys.platform`` guard. The simulation cannot flip
# ``sys.platform`` (click, psutil and pendulum break), so for exactly these importers the blocked import returns
# a stub. ``test_platform_guarded_modules_guard_unix_imports`` checks that each Unix-only import in them sits in the
# branch of a ``sys.platform`` test that is not taken on Windows.
PLATFORM_GUARDED = ("dango.utils.dbt_lock", "dango.platform.scheduling.jobs")

# Third-party packages that choose a POSIX backend by ``sys.platform`` (Windows loads its own): they are imported
# for real BEFORE the Unix-only modules are blocked, so no dango module is ever excused by a third-party failure.
PREIMPORT_THIRD_PARTY = (
    "psutil",  # Linux backend imports resource; Windows loads _pswindows
    "readchar",  # POSIX reader imports termios; Windows loads _win_read
    "mcp.server.stdio",  # imports fcntl under `if sys.platform != "win32"`
)

# Modules whose import must succeed in the walk, so a failing package __init__ cannot hide its submodules.
REQUIRED_IMPORTED = ("dango.web.app", "dango.notebooks.manager", "dango.cli.main")

# Python source run in the child: block the modules, install the guarded-importer wrapper, then import.
_PRELUDE = """
import builtins, json, sys, types
blocked = tuple(json.loads(sys.argv[1]))
guarded = tuple(json.loads(sys.argv[2]))
import importlib as _il
for _pre in json.loads(sys.argv[3]):
    try:
        _il.import_module(_pre)
    except ImportError:
        pass
for _name in blocked:
    sys.modules[_name] = None
_real_import = builtins.__import__
def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if (
        level == 0
        and name in blocked
        and globals is not None
        and globals.get("__name__") in guarded
    ):
        return types.ModuleType(name)
    return _real_import(name, globals, locals, fromlist, level)
builtins.__import__ = _guarded_import
"""

_WALK = (
    _PRELUDE
    + """
import importlib, pkgutil, traceback
server_only = tuple(json.loads(sys.argv[4]))
out_path = sys.argv[5]
import dango

def root_frame(exc):
    while exc.__cause__ is not None or (exc.__context__ is not None and not exc.__suppress_context__):
        exc = exc.__cause__ or exc.__context__
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if not f.filename.startswith("<")]
    return exc, (frames[-1] if frames else None)

failures, imported, optional_missing = [], [], []
def record(name, exc):
    root, frame = root_frame(exc)
    where = f"{frame.filename.replace(chr(92), '/')}:{frame.lineno}" if frame else ""
    missing_dep = (
        isinstance(root, ImportError) or type(root).__name__ == "MissingDependencyException"
    ) and getattr(root, "name", None) not in blocked and "halted" not in str(root)
    if name.startswith("dango.ingestion.dlt_sources.") and missing_dep:
        optional_missing.append(name)
        return
    entry = [name, type(root).__name__, str(root).splitlines()[0] if str(root) else "", where]
    if entry not in failures:
        failures.append(entry)

def on_error(name):
    record(name, sys.exc_info()[1])

for info in pkgutil.walk_packages(dango.__path__, prefix="dango.", onerror=on_error):
    if info.name.startswith(server_only):
        continue
    try:
        importlib.import_module(info.name)
        imported.append(info.name)
    except BaseException as exc:
        record(info.name, exc)

with open(out_path, "w", encoding="utf-8") as handle:
    json.dump({"failures": failures, "imported": imported, "optional_missing": optional_missing}, handle)
"""
)


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    return env


def _run_child(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=REPO_ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )


@pytest.mark.unit
def test_every_local_module_imports_without_unix_only_modules(tmp_path: Path) -> None:
    out = tmp_path / "walk.json"
    proc = _run_child(
        _WALK,
        json.dumps(UNIX_ONLY_MODULES),
        json.dumps(PLATFORM_GUARDED),
        json.dumps(PREIMPORT_THIRD_PARTY),
        json.dumps(SERVER_ONLY_PACKAGES),
        str(out),
    )
    assert out.exists(), f"walk child crashed:\n{proc.stderr[-2000:]}"
    result = json.loads(out.read_text(encoding="utf-8"))
    failures = result["failures"]
    assert failures == [], "modules that fail to import without Unix-only modules:\n" + "\n".join(
        f"  {n}: {t}: {m} (at {w})" for n, t, m, w in failures
    )
    missing = [m for m in REQUIRED_IMPORTED if m not in result["imported"]]
    assert missing == [], (
        f"walk did not import {missing}: a failing package __init__ hides its submodules"
    )


@pytest.mark.unit
def test_access_token_imports_without_fcntl() -> None:
    script = (
        _PRELUDE + "import dango.notebooks.access_token\nimport dango.web.app\nprint('IMPORTED')\n"
    )
    proc = _run_child(
        script,
        json.dumps(["fcntl"]),
        json.dumps(PLATFORM_GUARDED),
        json.dumps(PREIMPORT_THIRD_PARTY),
    )
    assert proc.returncode == 0 and "IMPORTED" in proc.stdout, proc.stderr[-2000:]


def _eval_windows(node: ast.expr) -> bool:
    """Evaluate a ``sys.platform`` test as if running on Windows; ValueError for anything unsupported."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_windows(node.operand)
    if isinstance(node, ast.BoolOp):
        values = [_eval_windows(v) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and ast.unparse(node.left) == "sys.platform"
    ):
        right, op = node.comparators[0], node.ops[0]
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            if isinstance(op, ast.Eq):
                return right.value == "win32"
            if isinstance(op, ast.NotEq):
                return right.value != "win32"
        if isinstance(right, ast.Tuple | ast.List) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in right.elts
        ):
            members = {e.value for e in right.elts if isinstance(e, ast.Constant)}
            if isinstance(op, ast.In):
                return "win32" in members
            if isinstance(op, ast.NotIn):
                return "win32" not in members
    raise ValueError(f"unsupported platform test: {ast.unparse(node)}")


def _guarded_for_windows(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when *node* is guarded: an enclosing ``if`` on sys.platform leaves it out of the Windows branch."""
    child, current = node, parents.get(node)
    while current is not None:
        if isinstance(current, ast.If) and "sys.platform" in ast.unparse(current.test):
            try:
                test_is_true = _eval_windows(current.test)
            except ValueError:
                test_is_true = None
            in_body = child in current.body
            if test_is_true is not None and in_body != test_is_true:
                return True
        child, current = current, parents.get(current)
    return False


def unix_only_imports(source: str) -> list[tuple[int, bool]]:
    """(line, guarded) for every import of a Unix-only module in *source*.

    ``guarded`` means an enclosing ``if sys.platform ...`` puts the import in the branch not taken on Windows.
    """
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found: list[tuple[int, bool]] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module.split(".")[0]]
        if any(name in UNIX_ONLY_MODULES for name in names):
            found.append((node.lineno, _guarded_for_windows(node, parents)))
    return found


@pytest.mark.unit
@pytest.mark.parametrize("module", PLATFORM_GUARDED)
def test_platform_guarded_modules_guard_unix_imports(module: str) -> None:
    source = (REPO_ROOT / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8")
    found = unix_only_imports(source)
    assert found, f"{module} has no Unix-only import; drop it from PLATFORM_GUARDED"
    unguarded = [line for line, guarded in found if not guarded]
    assert unguarded == [], (
        f"{module}: Unix-only import is not in the non-Windows branch of an `if sys.platform` at lines {unguarded}"
    )


_ACCEPTED = (
    'import sys\nif sys.platform == "win32":\n    import msvcrt\nelse:\n    import fcntl\n',
    'import sys\nif sys.platform != "win32":\n    import fcntl\n',
    'import sys\nif sys.platform == "linux":\n    import termios\n',
    'import sys\nif sys.platform in ("linux", "darwin"):\n    import pwd\n',
)
_REJECTED = (
    'import sys\nif sys.platform != "darwin":\n    import termios\n',
    'import sys\nif sys.platform == "win32":\n    import fcntl\nelse:\n    import msvcrt\n',
    "import termios\n",
    "if True:\n    import termios\n",
    'import sys\nif sys.platform.startswith("linux"):\n    import termios\n',
)


@pytest.mark.unit
@pytest.mark.parametrize("source", _ACCEPTED)
def test_ast_guard_accepts_correct_platform_guards(source: str) -> None:
    found = unix_only_imports(source)
    assert found and all(guarded for _line, guarded in found)


@pytest.mark.unit
@pytest.mark.parametrize("source", _REJECTED)
def test_ast_guard_rejects_imports_that_run_on_windows(source: str) -> None:
    found = unix_only_imports(source)
    assert found and not all(guarded for _line, guarded in found)
