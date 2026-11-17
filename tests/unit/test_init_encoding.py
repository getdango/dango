"""tests/unit/test_init_encoding.py

`dango init` works under a cp1252 environment and its rollback releases the log file.
"""

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
from dango.cli.init_rollback import take_snapshot
from dango.logging import _DangoFileHandler, configure_logging

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


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
        # A runner whose NO_PROXY covers github.com would otherwise bypass the dummy proxy.
        "NO_PROXY": "",
        "no_proxy": "",
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
    initializer = ProjectInitializer(tmp_path)
    initializer._snapshot = take_snapshot(tmp_path)
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

    initializer._rollback_initialization()

    assert seen and all(seen)
    assert handlers[0].stream is None
    assert not [h for h in root.handlers if isinstance(h, _DangoFileHandler)]
    assert not (tmp_path / ".dango").exists()
