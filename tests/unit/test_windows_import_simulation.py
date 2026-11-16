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
# a stub. ``test_platform_guarded_modules_guard_unix_imports`` proves the allowlist cannot hide an unguarded import.
PLATFORM_GUARDED = ("dango.utils.dbt_lock", "dango.platform.scheduling.jobs")

# (dango module prefix, failing third-party frame path suffix, reason). An entry only excuses a failure whose
# root-cause traceback frame ends with the frame suffix; a failure inside dango/ is never excused.
THIRD_PARTY_ARTEFACTS: tuple[tuple[str, str, str], ...] = (
    (
        "dango.cli.",
        "readchar/_posix_read.py",
        "readchar picks its POSIX reader by sys.platform and imports termios (Windows uses _win_read)",
    ),
    (
        "dango.cli.",
        "mcp/server/stdio.py",
        "mcp imports fcntl under `if sys.platform != 'win32'`, which the simulation cannot flip",
    ),
)

# Python source run in the child: block the modules, install the guarded-importer wrapper, then import.
_PRELUDE = """
import builtins, json, sys, types
blocked = tuple(json.loads(sys.argv[1]))
guarded = tuple(json.loads(sys.argv[2]))
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
server_only = tuple(json.loads(sys.argv[3]))
artefacts = [tuple(item) for item in json.loads(sys.argv[4])]
out_path = sys.argv[5]
import dango

def root_frame(exc):
    while exc.__cause__ is not None or (exc.__context__ is not None and not exc.__suppress_context__):
        exc = exc.__cause__ or exc.__context__
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if not f.filename.startswith("<")]
    return exc, (frames[-1] if frames else None)

failures, excused, optional_missing = [], [], []
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
    for prefix, suffix, _reason in artefacts:
        frame_file = where.rsplit(":", 1)[0]
        if name.startswith(prefix) and frame_file.endswith(suffix):
            if entry not in excused:
                excused.append(entry)
            return
    if entry not in failures:
        failures.append(entry)

def on_error(name):
    record(name, sys.exc_info()[1])

for info in pkgutil.walk_packages(dango.__path__, prefix="dango.", onerror=on_error):
    if info.name.startswith(server_only):
        continue
    try:
        importlib.import_module(info.name)
    except BaseException as exc:
        record(info.name, exc)

with open(out_path, "w", encoding="utf-8") as handle:
    json.dump({"failures": failures, "excused": excused, "optional_missing": optional_missing}, handle)
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
        json.dumps(SERVER_ONLY_PACKAGES),
        json.dumps(THIRD_PARTY_ARTEFACTS),
        str(out),
    )
    assert out.exists(), f"walk child crashed:\n{proc.stderr[-2000:]}"
    result = json.loads(out.read_text(encoding="utf-8"))
    failures = result["failures"]
    assert failures == [], "modules that fail to import without Unix-only modules:\n" + "\n".join(
        f"  {n}: {t}: {m} (at {w})" for n, t, m, w in failures
    )
    # Every excusal must be real: an artefact entry that no longer matches anything is stale.
    for prefix, suffix, _reason in THIRD_PARTY_ARTEFACTS:
        assert any(
            n.startswith(prefix) and w.rsplit(":", 1)[0].endswith(suffix)
            for n, _t, _m, w in result["excused"]
        ), f"stale THIRD_PARTY_ARTEFACTS entry {prefix!r} / {suffix!r}"


@pytest.mark.unit
def test_access_token_imports_without_fcntl() -> None:
    script = (
        _PRELUDE + "import dango.notebooks.access_token\nimport dango.web.app\nprint('IMPORTED')\n"
    )
    proc = _run_child(script, json.dumps(["fcntl"]), json.dumps(PLATFORM_GUARDED))
    assert proc.returncode == 0 and "IMPORTED" in proc.stdout, proc.stderr[-2000:]


def _guarded_by_platform_check(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.If) and "sys.platform" in ast.unparse(current.test):
            return True
        current = parents.get(current)
    return False


@pytest.mark.unit
@pytest.mark.parametrize("module", PLATFORM_GUARDED)
def test_platform_guarded_modules_guard_unix_imports(module: str) -> None:
    source = (REPO_ROOT / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    unix_imports = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module.split(".")[0]]
        if any(name in UNIX_ONLY_MODULES for name in names):
            unix_imports.append(node)
    assert unix_imports, f"{module} has no Unix-only import; drop it from PLATFORM_GUARDED"
    unguarded = [n.lineno for n in unix_imports if not _guarded_by_platform_check(n, parents)]
    assert unguarded == [], (
        f"{module}: Unix-only import not under an `if sys.platform` guard at lines {unguarded}"
    )
