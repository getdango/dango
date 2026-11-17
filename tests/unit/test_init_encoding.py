"""tests/unit/test_init_encoding.py

`dango init` works under a cp1252 environment and its rollback releases the log file.
"""

import ast
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

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
    tmp_path: Path, restore_root_logging: None
) -> None:
    configure_logging(log_dir=tmp_path / ".dango" / "logs")
    root = logging.getLogger()
    handlers = [h for h in root.handlers if isinstance(h, _DangoFileHandler)]
    assert len(handlers) == 1
    logging.getLogger("dango.test").warning("opens the log file")
    assert handlers[0].stream is not None

    ProjectInitializer(tmp_path)._rollback_initialization()

    assert handlers[0].stream is None
    assert not [h for h in root.handlers if isinstance(h, _DangoFileHandler)]
    assert not (tmp_path / ".dango").exists()


def _missing_encoding(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"write_text", "read_text"}:
            kind = func.attr
            mode_arg = None
        elif isinstance(func, ast.Name) and func.id == "open":
            kind, mode_arg = "open", node.args[1] if len(node.args) > 1 else None
        elif isinstance(func, ast.Attribute) and func.attr == "fdopen":
            kind, mode_arg = "fdopen", node.args[1] if len(node.args) > 1 else None
        else:
            continue
        for kw in node.keywords:
            if kw.arg == "mode":
                mode_arg = kw.value
        if (
            kind in {"open", "fdopen"}
            and isinstance(mode_arg, ast.Constant)
            and "b" in str(mode_arg.value)
        ):
            continue
        if not any(kw.arg == "encoding" for kw in node.keywords):
            problems.append(f"{path.name}:{node.lineno} {kind}() without encoding=")
    return problems


@pytest.mark.parametrize("rel", INIT_PATH_FILES)
def test_init_path_text_io_names_its_encoding(rel: str) -> None:
    path = REPO_ROOT / rel
    assert path.is_file(), rel
    assert _missing_encoding(path) == []
