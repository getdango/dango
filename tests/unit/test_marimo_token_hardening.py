"""tests/unit/test_marimo_token_hardening.py

Local Marimo token hardening: private log without the token, token only for executors, start lock.
"""

from __future__ import annotations

import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.auth.models import Role

_M = "dango.notebooks.manager"
_CLOUD = "dango.config.helpers.is_running_on_cloud"


def _token(root: Path) -> str:
    return (root / ".dango" / "state" / "marimo.token").read_text()


def _marimo_stub(seen: dict[str, Any], *, delay: float = 0.0) -> Any:
    """Popen stand-in that prints Marimo's startup URL (with the token) unless ``--quiet``."""

    def _popen(cmd: list[str], **kw: Any) -> MagicMock:
        seen.setdefault("cmds", []).append(list(cmd))
        seen["popen_calls"] = seen.get("popen_calls", 0) + 1
        if delay:
            time.sleep(delay)
        if "--token-password-file" in cmd and "--quiet" not in cmd:
            token = Path(cmd[cmd.index("--token-password-file") + 1]).read_text()
            kw["stdout"].write(f"URL: http://127.0.0.1:7805?access_token={token}\n")
            kw["stdout"].flush()
        proc = MagicMock()
        proc.pid = 12345
        proc.poll.return_value = None
        return proc

    return _popen


@contextmanager
def _patched(root: Path, popen: Any) -> Iterator[None]:
    """Patch process/readiness plumbing once (never from racing threads)."""
    (root / ".dango").mkdir(parents=True, exist_ok=True)
    with (
        patch(f"{_M}.subprocess.Popen", side_effect=popen),
        patch(f"{_M}._get_idle_timeout", return_value=7200),
        patch(f"{_M}._is_marimo_responding", return_value=True),
        patch(
            f"{_M}.is_process_running",
            side_effect=lambda pid, expected_start_time=None: (
                pid == 12345 and (root / ".dango" / "marimo.pid").exists()
            ),
        ),
        patch(f"{_M}.time.sleep"),
        patch(_CLOUD, return_value=False),
    ):
        yield


def _run_start(root: Path, popen: Any) -> int | None:
    from dango.notebooks.manager import start_marimo

    with _patched(root, popen):
        return start_marimo(root, port=7805)


@pytest.mark.unit
def test_log_never_contains_token_and_is_private(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    _run_start(tmp_path, _marimo_stub(seen))
    cmd = seen["cmds"][0]
    assert cmd.index("--quiet") < cmd.index("edit"), "--quiet is a global marimo option"
    log = tmp_path / ".dango" / "marimo.log"
    assert _token(tmp_path) not in log.read_text()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


@pytest.mark.unit
def test_existing_world_readable_log_is_tightened(tmp_path: Path) -> None:
    log = tmp_path / ".dango" / "marimo.log"
    log.parent.mkdir(parents=True)
    log.write_text("old contents\n")
    log.chmod(0o644)
    _run_start(tmp_path, _marimo_stub({}))
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert "old contents" not in log.read_text()


@pytest.mark.unit
def test_log_is_not_written_through_a_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "marimo.log").symlink_to(victim)
    try:
        _run_start(tmp_path, _marimo_stub({}))
    except RuntimeError:
        pass
    assert victim.read_text() == "keep"


@pytest.mark.unit
def test_cloud_log_unchanged_no_quiet(tmp_path: Path) -> None:
    from dango.notebooks.manager import start_marimo

    seen: dict[str, Any] = {}
    (tmp_path / ".dango").mkdir()
    with (
        patch(f"{_M}.subprocess.Popen", side_effect=_marimo_stub(seen)),
        patch(f"{_M}._get_idle_timeout", return_value=7200),
        patch(f"{_M}._is_marimo_responding", return_value=True),
        patch(f"{_M}.is_process_running", return_value=False),
        patch(f"{_M}.time.sleep"),
        patch(_CLOUD, return_value=True),
    ):
        start_marimo(tmp_path, port=7805)
    assert "--quiet" not in seen["cmds"][0]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("role", "has_token"),
    [(Role.VIEWER, False), (Role.EDITOR, True), (Role.ADMIN, True)],
)
def test_status_url_token_only_for_executors(tmp_path: Path, role: Role, has_token: bool) -> None:
    from tests.unit.test_web_notebooks import _client

    _run_start(tmp_path, _marimo_stub({}))
    p = "dango.web.routes.notebooks"
    with (
        patch(f"{p}.get_project_root", return_value=tmp_path),
        patch(f"{p}.get_marimo_status", return_value={"running": True, "port": 7805, "pid": 1}),
        patch(_CLOUD, return_value=False),
    ):
        resp = _client(tmp_path, role=role).get("/api/notebooks/status?file=nb")
    assert resp.status_code == 200
    url = resp.json()["marimo_url"]
    assert url.startswith("http://localhost:7805/?file=nb")
    assert ("access_token=" in url) is has_token
    assert _token(tmp_path) not in resp.text or has_token


@pytest.mark.unit
def test_concurrent_starts_spawn_one_marimo_and_keep_the_token(tmp_path: Path) -> None:
    from dango.notebooks.manager import start_marimo

    seen: dict[str, Any] = {}
    popen = _marimo_stub(seen, delay=0.3)
    outcomes: list[Any] = []

    def _worker() -> None:
        try:
            outcomes.append(start_marimo(tmp_path, port=7805))
        except Exception as exc:
            outcomes.append(exc)

    with _patched(tmp_path, popen):
        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
    assert not any(t.is_alive() for t in threads)
    assert seen["popen_calls"] == 1
    assert sum(1 for o in outcomes if o == 12345) == 1
    assert [o for o in outcomes if isinstance(o, RuntimeError)], outcomes
    started_token = Path(seen["cmds"][0][seen["cmds"][0].index("--token-password-file") + 1])
    assert started_token.exists()
    assert (tmp_path / ".dango" / "state" / "marimo.start.lock").exists()
