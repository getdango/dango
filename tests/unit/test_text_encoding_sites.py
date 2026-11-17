"""tests/unit/test_text_encoding_sites.py

Every text-mode file open and text subprocess in the CLI/config/utils packages names its encoding.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

SCANNED = [
    "dango/cli",
    "dango/telemetry.py",
    "dango/config",
    "dango/utils",
    "dango/transformation",
    "dango/ingestion",
    "dango/oauth",
]
# dlt_sources is out of scope for this package; platform/cloud and runtime are server-only.
EXCLUDED_PARTS = ("dango/ingestion/dlt_sources",)

# Receivers whose ``.open`` is not a text-file open.
_NOT_TEXT_OPENERS = {
    "os",
    "opener",
    "gzip",
    "bz2",
    "lzma",
    "tarfile",
    "zipfile",
    "shelve",
    "dbm",
    "webbrowser",
    "subprocess",
    "request",
    "urllib",
    "Image",
}
_TEMP_FILES = {"NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"}
_FILE_HANDLERS = {"FileHandler", "RotatingFileHandler", "TimedRotatingFileHandler"}
_PARSERS = {"ConfigParser", "RawConfigParser", "SafeConfigParser"}
_SUBPROCESS = {"run", "Popen", "check_output", "check_call", "call"}


def _py_files() -> list[Path]:
    files: list[Path] = []
    for entry in SCANNED:
        base = REPO_ROOT / entry
        found = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in found:
            rel = path.relative_to(REPO_ROOT).as_posix()
            if not any(rel.startswith(part) for part in EXCLUDED_PARTS):
                files.append(path)
    return files


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _mode_of(node: ast.Call, positional: int) -> ast.expr | None:
    mode = node.args[positional] if len(node.args) > positional else None
    for kw in node.keywords:
        if kw.arg == "mode":
            mode = kw.value
    return mode


def _is_binary(mode: ast.expr | None) -> bool:
    return isinstance(mode, ast.Constant) and "b" in str(mode.value)


def _keywords(node: ast.Call) -> set[str]:
    return {kw.arg for kw in node.keywords if kw.arg}


def _truthy(node: ast.Call, name: str) -> bool:
    for kw in node.keywords:
        if kw.arg == name:
            return not (isinstance(kw.value, ast.Constant) and not kw.value.value)
    return False


def _parser_names(tree: ast.AST) -> set[str]:
    """Names (plain or ``self.x``) assigned from a ConfigParser constructor."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        value = node.value
        if not (isinstance(value, ast.Call) and _name(value.func) in _PARSERS):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names.update(n for t in targets if (n := _name(t)))
    return names


def _call_problem(node: ast.Call, parsers: set[str]) -> str | None:
    func = node.func
    attr = func.attr if isinstance(func, ast.Attribute) else None
    recv = _name(func.value) if isinstance(func, ast.Attribute) else None
    fname = func.id if isinstance(func, ast.Name) else None
    keywords = _keywords(node)

    if attr in {"write_text", "read_text"}:
        # read_text(encoding) / write_text(data, encoding) may pass it positionally.
        named = "encoding" in keywords or len(node.args) >= (1 if attr == "read_text" else 2)
        return None if named else f"{attr}() without encoding="
    if fname == "open" or (attr == "open" and recv not in _NOT_TEXT_OPENERS):
        positional = 1 if (fname == "open" or recv == "io") else 0
        if _is_binary(_mode_of(node, positional)) or "encoding" in keywords:
            return None
        if len(node.args) > positional + 2:  # encoding passed positionally
            return None
        return "open() without encoding="
    if attr == "fdopen":
        if _is_binary(_mode_of(node, 1)) or "encoding" in keywords:
            return None
        return "fdopen() without encoding="
    if attr == "read" and recv in parsers:
        return None if "encoding" in keywords else "ConfigParser.read() without encoding="
    if attr in _TEMP_FILES or fname in _TEMP_FILES:
        mode = _mode_of(node, 0)
        if mode is None or _is_binary(mode) or "encoding" in keywords:
            return None
        return "text temporary file without encoding="
    if attr in _FILE_HANDLERS or fname in _FILE_HANDLERS:
        return None if "encoding" in keywords else "logging file handler without encoding="
    if (recv == "subprocess" and attr in _SUBPROCESS) or fname in {"Popen", "check_output"}:
        if _truthy(node, "text") or _truthy(node, "universal_newlines"):
            if not {"encoding", "errors"} <= keywords:
                return "text subprocess without encoding= and errors="
    return None


def missing_encoding(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parsers = _parser_names(tree)
    rel = path.relative_to(REPO_ROOT).as_posix()
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (problem := _call_problem(node, parsers)):
            problems.append(f"{rel}:{node.lineno} {problem}")
    return problems


def test_scanner_covers_the_package() -> None:
    assert len(_py_files()) > 100
    assert not any("dlt_sources" in p.as_posix() for p in _py_files())


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_text_io_names_its_encoding(path: Path) -> None:
    assert missing_encoding(path) == []
