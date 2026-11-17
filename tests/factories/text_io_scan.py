"""tests/factories/text_io_scan.py

AST scanner for text-mode file I/O and text subprocesses that do not name their encoding or newline.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Server-only code (runs on Linux servers) and vendored dlt sources are out of scope.
EXCLUDED_PREFIXES = (
    "dango/platform/cloud",
    "dango/runtime",
    "dango/ingestion/dlt_sources",
)

_MODE_RE = re.compile(r"^[rwaxbtU+]+$")
_FILE_FUNCS = {
    # dotted name -> (mode index, encoding index, newline index)
    "open": (1, 3, 4),
    "io.open": (1, 3, 4),
    "builtins.open": (1, 3, 4),
    "codecs.open": (1, 2, None),
    "os.fdopen": (1, 3, 4),
    "tempfile.NamedTemporaryFile": (0, 2, 3),
    "tempfile.TemporaryFile": (0, 2, 3),
    "tempfile.SpooledTemporaryFile": (1, 3, 4),
}
_HANDLERS = {"FileHandler", "RotatingFileHandler", "TimedRotatingFileHandler", "WatchedFileHandler"}
_SUBPROCESS = {"run", "Popen", "check_output", "check_call", "call"}
_PARSERS = {"ConfigParser", "RawConfigParser", "SafeConfigParser"}


def _imports(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    """Map local names to the dotted origin they were imported from; plus imported modules."""
    names: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                names[local] = alias.name if alias.asname else local
                modules.add(local)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names, modules


def _dotted(node: ast.expr, names: dict[str, str]) -> str | None:
    """Resolve ``a.b.c`` (through import aliases) to its dotted origin name."""
    if isinstance(node, ast.Name):
        return names.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, names)
        return f"{base}.{node.attr}" if base else None
    return None


def _arg(node: ast.Call, index: int | None, keyword: str) -> ast.expr | None:
    for kw in node.keywords:
        if kw.arg == keyword:
            return kw.value
    if index is not None and len(node.args) > index:
        return node.args[index]
    return None


def _opaque(node: ast.Call) -> bool:
    """True when ``*args``/``**kwargs`` could carry the argument being checked."""
    return any(isinstance(a, ast.Starred) for a in node.args) or any(
        kw.arg is None for kw in node.keywords
    )


def _mode_text(mode: ast.expr | None) -> str | None:
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return mode.value if _MODE_RE.match(mode.value) else None
    return None


def _truthy_kw(node: ast.Call, name: str) -> bool:
    for kw in node.keywords:
        if kw.arg == name:
            return not (isinstance(kw.value, ast.Constant) and not kw.value.value)
    return False


def _is_lf(value: ast.expr | None) -> bool:
    return isinstance(value, ast.Constant) and value.value == "\n"


def _parser_names(tree: ast.AST) -> set[str]:
    """Names (``x`` or ``self.x``) assigned from a ConfigParser constructor."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        value = node.value
        if not (isinstance(value, ast.Call) and _tail(value.func) in _PARSERS):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        found.update(n for t in targets if (n := _tail(t)))
    return found


def _tail(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _kind(node: ast.Call, names: dict[str, str], modules: set[str], parsers: set[str]):
    """Classify a call: (label, mode, encoding arg, newline arg, is_write) or None."""
    func = node.func
    dotted = _dotted(func, names)
    if dotted in _FILE_FUNCS:
        m, e, n = _FILE_FUNCS[dotted]
        mode = _arg(node, m, "mode")
        if dotted.startswith("tempfile.") and mode is None:
            return None  # default mode is binary
        return dotted, mode, _arg(node, e, "encoding"), _arg(node, n, "newline"), True
    if isinstance(func, ast.Attribute):
        root = func.value
        while isinstance(root, ast.Attribute | ast.Call):
            root = root.value if isinstance(root, ast.Attribute) else root.func
        from_module = isinstance(root, ast.Name) and root.id in modules and root.id != "self"
        if func.attr == "read_text" and not from_module:
            return "read_text", None, _arg(node, 0, "encoding"), None, False
        if func.attr == "write_text" and not from_module:
            return (
                "write_text",
                ast.Constant("w"),
                _arg(node, 1, "encoding"),
                _arg(node, 3, "newline"),
                True,
            )
        if func.attr == "open" and not from_module:
            first = _arg(node, 0, "mode")
            # ``opener.open(url)`` / ``zipfile.open("member")`` are not Path.open(mode).
            if first is not None and _mode_text(first) is None and not _mode_text_kw(node):
                return None
            return "Path.open", first, _arg(node, 2, "encoding"), _arg(node, 4, "newline"), True
        if func.attr == "read" and (
            _tail(func.value) in parsers
            or (isinstance(func.value, ast.Call) and _tail(func.value.func) in _PARSERS)
        ):
            return "ConfigParser.read", None, _arg(node, 1, "encoding"), None, False
    last = dotted.rsplit(".", 1)[-1] if dotted else None
    if last in _HANDLERS:
        return "logging file handler", None, _arg(node, 3, "encoding"), None, False
    if dotted == "logging.basicConfig" and _arg(node, None, "filename") is not None:
        return "logging.basicConfig(filename=)", None, _arg(node, None, "encoding"), None, False
    return None


def _mode_text_kw(node: ast.Call) -> bool:
    return any(kw.arg == "mode" for kw in node.keywords)


def _file_problems(node: ast.Call, kind: tuple, *, check_lf: bool) -> list[str]:
    label, mode, encoding, newline, writes = kind
    problems: list[str] = []
    text = _mode_text(mode)
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and text is None:
        return problems  # not a file mode
    if text is not None and "b" in text:
        return problems  # binary
    if encoding is None:
        problems.append(f"{label}() without encoding=")
    if check_lf and writes:
        wrote = label == "write_text" or (text is not None and re.search(r"[wax+]", text))
        if wrote and not _is_lf(newline):
            problems.append(f"{label}() text write without newline='\\n'")
    return problems


def _subprocess_problem(node: ast.Call, names: dict[str, str]) -> str | None:
    dotted = _dotted(node.func, names)
    if not dotted or dotted.rsplit(".", 1)[0] != "subprocess" or dotted[11:] not in _SUBPROCESS:
        return None
    if not (_truthy_kw(node, "text") or _truthy_kw(node, "universal_newlines")):
        return None
    keywords = {kw.arg for kw in node.keywords if kw.arg}
    constant_text = any(
        kw.arg in {"text", "universal_newlines"} and isinstance(kw.value, ast.Constant)
        for kw in node.keywords
    )
    if not constant_text and _opaque(node):
        return None  # conditional text mode carried through **kwargs
    if {"encoding", "errors"} <= keywords:
        return None
    return "text subprocess without encoding= and errors="


def scan_source(source: str, label: str = "<src>", *, check_lf: bool = True) -> list[str]:
    """Return ``label:line message`` for each unspecified text encoding / newline."""
    tree = ast.parse(source)
    names, modules = _imports(tree)
    parsers = _parser_names(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        problems: list[str] = []
        kind = _kind(node, names, modules, parsers)
        if kind is not None and not _opaque(node):
            problems.extend(_file_problems(node, kind, check_lf=check_lf))
        if (sub := _subprocess_problem(node, names)) is not None:
            problems.append(sub)
        found.extend(f"{label}:{node.lineno} {p}" for p in problems)
    return sorted(found)


def scanned_files() -> list[Path]:
    files = []
    for path in sorted((REPO_ROOT / "dango").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not rel.startswith(EXCLUDED_PREFIXES):
            files.append(path)
    return files


def scan_tree(*, check_lf: bool = True) -> list[str]:
    found: list[str] = []
    for path in scanned_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        found.extend(scan_source(path.read_text(encoding="utf-8"), rel, check_lf=check_lf))
    return found
