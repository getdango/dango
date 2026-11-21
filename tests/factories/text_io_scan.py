"""tests/factories/text_io_scan.py

AST scanner for text-mode file I/O and text subprocesses that do not name their encoding or newline.

Rules (local code only; the excluded prefixes are server-only or vendored):

* ``encoding`` must be the constant ``"utf-8"`` (``None``, ``"locale"``, other codecs and
  variables are rejected). ``errors``, when given to a file call, must be a string constant.
* A text subprocess (``text=True``/``universal_newlines=True``, ``getoutput``,
  ``getstatusoutput``) needs ``encoding="utf-8"`` and ``errors="replace"``.
* Every text write needs ``newline="\\n"`` (``codecs.open`` and ``TextIOWrapper`` have no
  such parameter or are not writes of a new file, so they are exempt).
* ``os.popen`` is always flagged (it decodes with the locale and has no encoding parameter).
* ``load_dotenv``, ``dotenv_values``, ``find_dotenv`` and ``DotEnv`` calls are flagged everywhere
  except ``dango/utils/project_env.py`` (``DOTENV_HELPER_MODULES``): they read the project
  ``.env`` and fail with a raw codec error on a non-UTF-8 file; use ``load_env``/``read_env``.

Keywords carried by ``**NAME`` (a module-level dict literal) or ``**{...}`` are followed.

Not detected (documented limits): an opaque ``**kwargs``/``*args`` (including wrappers that
forward them to ``open`` and calls like ``**decode(flag)``); ``p.open(mode_variable)``; a
``ConfigParser`` that is not assigned from its constructor in the same file; a dict built
at run time or passed through a function; anything reached through ``getattr``/``eval``.
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

# The only modules that may call python-dotenv's readers (the safe project ``.env`` loader).
DOTENV_HELPER_MODULES = ("dango/utils/project_env.py",)
_DOTENV_FUNCS = {"load_dotenv", "dotenv_values", "find_dotenv", "DotEnv"}

_MODE_RE = re.compile(r"^[rwaxbtU+]+$")
# dotted name -> (mode index, encoding index, errors index, newline index or None)
_FILE_FUNCS: dict[str, tuple[int, int, int, int | None]] = {
    "open": (1, 3, 4, 5),
    "io.open": (1, 3, 4, 5),
    "builtins.open": (1, 3, 4, 5),
    "codecs.open": (1, 2, 3, None),
    "os.fdopen": (1, 3, 4, 5),
    "tempfile.NamedTemporaryFile": (0, 2, 99, 3),
    "tempfile.TemporaryFile": (0, 2, 99, 3),
    "tempfile.SpooledTemporaryFile": (1, 3, 99, 4),
}
# handler class -> encoding index (verified against the CPython signatures)
_HANDLERS = {
    "FileHandler": 2,
    "WatchedFileHandler": 2,
    "RotatingFileHandler": 4,
    "TimedRotatingFileHandler": 4,
}
_SUBPROCESS = {"run", "Popen", "check_output", "check_call", "call"}
_SHELL_OUTPUT = {"subprocess.getoutput", "subprocess.getstatusoutput"}
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
    for node in ast.walk(tree):  # ``o = open`` / ``reader = io.open``
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name | ast.Attribute):
            origin = _dotted(node.value, names)
            if origin in _FILE_FUNCS:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names[target.id] = origin
    return names, modules


def _dotted(node: ast.expr, names: dict[str, str]) -> str | None:
    """Resolve ``a.b.c`` (through import aliases) to its dotted origin name."""
    if isinstance(node, ast.Name):
        return names.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, names)
        return f"{base}.{node.attr}" if base else None
    return None


def _module_dicts(tree: ast.Module) -> dict[str, dict[str, ast.expr]]:
    """Module-level ``NAME = {...}`` literals with constant string keys."""
    found: dict[str, dict[str, ast.expr]] = {}
    for node in tree.body:
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if not isinstance(value, ast.Dict):
            continue
        entries = _dict_entries(value)
        if entries is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                found[target.id] = entries
    return found


def _dict_entries(value: ast.Dict) -> dict[str, ast.expr] | None:
    entries: dict[str, ast.expr] = {}
    for key, val in zip(value.keys, value.values, strict=True):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None  # ``**other`` inside the literal, or a computed key
        entries[key.value] = val
    return entries


def _expand(node: ast.Call, dicts: dict[str, dict[str, ast.expr]]) -> ast.Call:
    """The call with resolvable ``**NAME`` / ``**{...}`` replaced by their keywords."""
    keywords: list[ast.keyword] = []
    for kw in node.keywords:
        entries: dict[str, ast.expr] | None = None
        if kw.arg is None:
            if isinstance(kw.value, ast.Dict):
                entries = _dict_entries(kw.value)
            elif isinstance(kw.value, ast.Name):
                entries = dicts.get(kw.value.id)
        if entries is None:
            keywords.append(kw)
        else:
            keywords.extend(ast.keyword(arg=k, value=v) for k, v in entries.items())
    return ast.Call(func=node.func, args=node.args, keywords=keywords)


def _arg(node: ast.Call, index: int | None, keyword: str) -> ast.expr | None:
    for kw in node.keywords:
        if kw.arg == keyword:
            return kw.value
    if index is not None and len(node.args) > index:
        return node.args[index]
    return None


def _has_kw(node: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in node.keywords)


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


def _is_const(value: ast.expr | None, expected: str | None = None) -> bool:
    if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
        return False
    return expected is None or value.value == expected


def _tail(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


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


def _kind(node: ast.Call, names: dict[str, str], modules: set[str], parsers: set[str]):
    """Classify a call: (label, mode, encoding, errors, newline, newline_applies, is_write)."""
    func = node.func
    dotted = _dotted(func, names)
    if dotted in _FILE_FUNCS:
        m, e, er, n = _FILE_FUNCS[dotted]
        mode = _arg(node, m, "mode")
        if dotted.startswith("tempfile.") and mode is None:
            return None  # default mode is binary
        enc, err, newline = _arg(node, e, "encoding"), _arg(node, er, "errors"), None
        if n is not None:
            newline = _arg(node, n, "newline")
        return dotted, mode, enc, err, newline, n is not None, True
    if dotted == "io.TextIOWrapper":
        return dotted, None, _arg(node, 1, "encoding"), _arg(node, 2, "errors"), None, False, False
    if isinstance(func, ast.Attribute):
        root = func.value
        while isinstance(root, ast.Attribute | ast.Call):
            root = root.value if isinstance(root, ast.Attribute) else root.func
        # ``pathlib.Path(x).read_text()`` is a Path method; ``gzip.open`` is a module function.
        root_name = root.id if isinstance(root, ast.Name) else None
        from_module = (
            root_name in modules and root_name != "self" and names.get(root_name) != "pathlib"
        )
        enc0, err1 = _arg(node, 0, "encoding"), _arg(node, 1, "errors")
        if func.attr == "read_text" and not from_module:
            return "read_text", None, enc0, err1, None, False, False
        if func.attr == "write_text" and not from_module:
            enc, err, newline = _arg(node, 1, "encoding"), _arg(node, 2, "errors"), None
            newline = _arg(node, 3, "newline")
            return "write_text", ast.Constant("w"), enc, err, newline, True, True
        if func.attr == "open" and not from_module:
            first = _arg(node, 0, "mode")
            # ``opener.open(url)`` / ``zipfile.open("member")`` are not Path.open(mode).
            if first is not None and _mode_text(first) is None and not _has_kw(node, "mode"):
                return None
            enc, err = _arg(node, 2, "encoding"), _arg(node, 3, "errors")
            return "Path.open", first, enc, err, _arg(node, 4, "newline"), True, True
        if func.attr == "read" and (
            _tail(func.value) in parsers
            or (isinstance(func.value, ast.Call) and _tail(func.value.func) in _PARSERS)
        ):
            return "ConfigParser.read", None, _arg(node, 1, "encoding"), None, None, False, False
    last = dotted.rsplit(".", 1)[-1] if dotted else None
    if last in _HANDLERS:
        enc = _arg(node, _HANDLERS[last], "encoding")
        return "logging file handler", None, enc, None, None, False, False
    if dotted == "logging.basicConfig" and _arg(node, None, "filename") is not None:
        enc = _arg(node, None, "encoding")
        return "logging.basicConfig(filename=)", None, enc, None, None, False, False
    return None


def _file_problems(kind: tuple, *, check_lf: bool) -> list[str]:
    label, mode, encoding, errors, newline, newline_applies, writes = kind
    problems: list[str] = []
    text = _mode_text(mode)
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and text is None:
        return problems  # not a file mode
    if text is not None and "b" in text:
        return problems  # binary
    if encoding is None:
        problems.append(f"{label}() without encoding=")
    elif not _is_const(encoding, "utf-8"):
        problems.append(f"{label}() encoding= is not the constant 'utf-8'")
    errors_is_default = isinstance(errors, ast.Constant) and errors.value is None
    if errors is not None and not errors_is_default and not _is_const(errors):
        problems.append(f"{label}() errors= is not a string constant")
    if check_lf and writes and newline_applies:
        wrote = label == "write_text" or (text is not None and re.search(r"[wax+]", text))
        if wrote and not _is_lf(newline):
            problems.append(f"{label}() text write without newline='\\n'")
    return problems


def _subprocess_problem(node: ast.Call, names: dict[str, str]) -> str | None:
    dotted = _dotted(node.func, names)
    if dotted in _SHELL_OUTPUT:
        text_mode = True  # always decodes
    elif dotted and dotted.rsplit(".", 1)[0] == "subprocess" and dotted[11:] in _SUBPROCESS:
        text_mode = _truthy_kw(node, "text") or _truthy_kw(node, "universal_newlines")
    else:
        return None
    if not text_mode:
        return None
    constant_text = dotted in _SHELL_OUTPUT or any(
        kw.arg in {"text", "universal_newlines"} and isinstance(kw.value, ast.Constant)
        for kw in node.keywords
    )
    if not constant_text and _opaque(node):
        return None  # conditional text mode carried through opaque **kwargs
    encoding, errors = _arg(node, None, "encoding"), _arg(node, None, "errors")
    if _is_const(encoding, "utf-8") and _is_const(errors, "replace"):
        return None
    return "text subprocess without encoding='utf-8' and errors='replace'"


def _popen_problem(node: ast.Call, names: dict[str, str]) -> str | None:
    if _dotted(node.func, names) == "os.popen":
        return "os.popen() decodes with the locale; use subprocess with encoding="
    return None


def _dotenv_problem(node: ast.Call, names: dict[str, str]) -> str | None:
    dotted = _dotted(node.func, names)
    if dotted is None or dotted.rsplit(".", 1)[-1] not in _DOTENV_FUNCS:
        return None
    if not (dotted.startswith("dotenv.") or "." not in dotted):
        return None  # a method of some other object
    return (
        f"{dotted.rsplit('.', 1)[-1]}() reads a .env file outside "
        f"{DOTENV_HELPER_MODULES[0]}; use load_env()/read_env()"
    )


def _partial_problem(node: ast.Call, names: dict[str, str]) -> str | None:
    if _dotted(node.func, names) != "functools.partial" or not node.args:
        return None
    if _dotted(node.args[0], names) not in _FILE_FUNCS:
        return None
    if _is_const(_arg(node, None, "encoding"), "utf-8"):
        return None
    return "functools.partial(open) without encoding='utf-8'"


def scan_source(source: str, label: str = "<src>", *, check_lf: bool = True) -> list[str]:
    """Return ``label:line message`` for each unspecified or non-UTF-8 encoding / newline."""
    tree = ast.parse(source)
    names, modules = _imports(tree)
    parsers = _parser_names(tree)
    dicts = _module_dicts(tree)
    found: list[str] = []
    for original in ast.walk(tree):
        if not isinstance(original, ast.Call):
            continue
        node = _expand(original, dicts)
        problems: list[str] = []
        kind = _kind(node, names, modules, parsers)
        if kind is not None and not _opaque(node):
            problems.extend(_file_problems(kind, check_lf=check_lf))
        extras = [
            _subprocess_problem(node, names),
            _popen_problem(node, names),
            _partial_problem(node, names),
        ]
        if label not in DOTENV_HELPER_MODULES:
            extras.append(_dotenv_problem(node, names))
        for extra in extras:
            if extra is not None:
                problems.append(extra)
        found.extend(f"{label}:{original.lineno} {p}" for p in problems)
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
