"""tests/unit/test_init_encoding.py

`dango init` works under a cp1252 environment and its rollback releases the log file.
"""

import ast
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dango.cli.init import ProjectInitializer
from dango.logging import _DangoFileHandler, configure_logging

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# Files on the `dango init` path: every text read/write must name its encoding.
INIT_PATH_FILES = [
    "dango/cli/init.py",
    "dango/cli/env_helpers.py",
    "dango/config/credentials.py",
    "dango/config/loader.py",
    "dango/config/cloud_credentials.py",
    "dango/config/auth_loading.py",
    "dango/utils/driver.py",
    "dango/utils/process.py",
    "dango/transformation/generator.py",
    "dango/logging.py",
    "dango/utils/stdio.py",
]


def test_init_skip_wizard_succeeds_under_cp1252(tmp_path: Path) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    env = {
        **os.environ,
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONIOENCODING": "cp1252",
        "LC_ALL": "C",
        "DANGO_ADMIN_PASSWORD": "Test-pass-12345",
        "DANGO_TELEMETRY": "0",
        # Unreachable proxy: no real driver download happens during the test.
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:9",
        "http_proxy": "http://127.0.0.1:9",
    }
    (tmp_path / "home").mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "dango.cli.main", "init", "--skip-wizard"],
        cwd=workdir,
        env=env,
        capture_output=True,
        timeout=300,
    )
    tail = (proc.stdout + proc.stderr)[-2000:].decode("utf-8", errors="replace")
    assert proc.returncode == 0, tail
    assert b"Traceback" not in proc.stderr, tail

    claude_md = workdir / "CLAUDE.md"
    assert claude_md.is_file()
    claude_md.read_bytes().decode("utf-8")
    for path in workdir.rglob("*.md"):
        path.read_bytes().decode("utf-8")


@pytest.fixture
def restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in saved:
            root.removeHandler(handler)
            handler.close()
    root.handlers[:] = saved


def test_rollback_closes_dango_log_handler_before_removing_dango_dir(
    tmp_path: Path, restore_root_logging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_logging(log_dir=tmp_path / ".dango" / "logs")
    root = logging.getLogger()
    handlers = [h for h in root.handlers if isinstance(h, _DangoFileHandler)]
    assert len(handlers) == 1
    logging.getLogger("dango.test").warning("opens the log file")
    assert handlers[0].stream is not None

    real_rmtree = shutil.rmtree
    seen: list[bool] = []

    def checking_rmtree(target: Any, *args: Any, **kwargs: Any) -> None:
        # Windows refuses to delete an open file, so the handler must be closed first.
        seen.append(handlers[0].stream is None)
        assert not [h for h in root.handlers if isinstance(h, _DangoFileHandler)]
        real_rmtree(target, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", checking_rmtree)

    ProjectInitializer(tmp_path)._rollback_initialization()

    assert seen and all(seen)
    assert handlers[0].stream is None
    assert not [h for h in root.handlers if isinstance(h, _DangoFileHandler)]
    assert not (tmp_path / ".dango").exists()


_NOT_FILE_OPENERS = {"os", "opener", "gzip", "webbrowser", "subprocess"}
_TEMP_FILES = {"NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"}


def _mode_of(node: ast.Call, positional: int) -> ast.expr | None:
    mode = node.args[positional] if len(node.args) > positional else None
    for kw in node.keywords:
        if kw.arg == "mode":
            mode = kw.value
    return mode


def _is_binary(mode: ast.expr | None) -> bool:
    return isinstance(mode, ast.Constant) and "b" in str(mode.value)


def _missing_encoding(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else None
        recv = (
            func.value.id
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            else None
        )
        if attr in {"write_text", "read_text"}:
            kind, binary = attr, False
        elif isinstance(func, ast.Name) and func.id == "open":
            kind, binary = "open", _is_binary(_mode_of(node, 1))
        elif attr == "open" and recv not in _NOT_FILE_OPENERS:
            # Path.open(mode) / io.open(path, mode)
            kind, binary = "open", _is_binary(_mode_of(node, 1 if recv == "io" else 0))
        elif attr == "fdopen":
            kind, binary = "fdopen", _is_binary(_mode_of(node, 1))
        elif attr == "read" and recv is not None and "config" in recv.lower():
            kind, binary = "configparser.read", False
        elif attr in _TEMP_FILES or (isinstance(func, ast.Name) and func.id in _TEMP_FILES):
            mode = _mode_of(node, 0)
            if mode is None or _is_binary(mode):
                continue
            kind, binary = "temporary file", False
        else:
            continue
        if binary:
            continue
        if not any(kw.arg == "encoding" for kw in node.keywords):
            problems.append(f"{path.name}:{node.lineno} {kind}() without encoding=")
    return problems


@pytest.mark.parametrize("rel", INIT_PATH_FILES)
def test_init_path_text_io_names_its_encoding(rel: str) -> None:
    path = REPO_ROOT / rel
    assert path.is_file(), rel
    assert _missing_encoding(path) == []


# Generated files (scripts, Dockerfile, YAML, .gitignore, .env) must be written with LF
# on every platform; on POSIX newline= is a no-op, so only a static check protects it.
_LF_FILES = {
    "dango/cli/init.py": {
        "gitignore_path",
        "config_path",
        "workflow_path",
        "dockerfile_path",
        "entrypoint_path",
        "dbt_project_path",
        "profiles_path",
    },
    "dango/config/credentials.py": {"gitignore_path"},
    "dango/config/loader.py": {"temp_path"},
    "dango/transformation/generator.py": {"sources_file", "staging_schema_file"},
}


def _writes_without_lf(path: Path, names: set[str]) -> tuple[int, list[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    checked, problems = 0, []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "open" or not node.args or len(node.args) < 2:
            continue
        target, mode = node.args[0], node.args[1]
        if not (isinstance(target, ast.Name) and target.id in names):
            continue
        if not (isinstance(mode, ast.Constant) and str(mode.value) in {"w", "a"}):
            continue
        checked += 1
        newline = next((kw.value for kw in node.keywords if kw.arg == "newline"), None)
        if not (isinstance(newline, ast.Constant) and newline.value == "\n"):
            problems.append(f"{path.name}:{node.lineno} write of {target.id} without newline='\\n'")
    return checked, problems


@pytest.mark.parametrize("rel", sorted(_LF_FILES))
def test_generated_files_are_written_with_lf(rel: str) -> None:
    checked, problems = _writes_without_lf(REPO_ROOT / rel, _LF_FILES[rel])
    assert checked > 0
    assert problems == []


def test_env_file_writes_use_lf() -> None:
    tree = ast.parse((REPO_ROOT / "dango/cli/env_helpers.py").read_text(encoding="utf-8"))
    writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fdopen"
    ]
    assert writes
    for node in writes:
        newline = next((kw.value for kw in node.keywords if kw.arg == "newline"), None)
        assert isinstance(newline, ast.Constant) and newline.value == "\n", node.lineno
