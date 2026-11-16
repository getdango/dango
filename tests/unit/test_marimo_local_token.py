"""tests/unit/test_marimo_local_token.py

Local Marimo runs with a per-start random token (file, mode 0600); cloud keeps --no-token.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_M = "dango.notebooks.manager"
_CLOUD = "dango.config.helpers.is_running_on_cloud"
_STATE = "dango.platform.run_as.isolation_state"


def _token_file(root: Path) -> Path:
    return root / ".dango" / "state" / "marimo.token"


def _start(root: Path, *, cloud: bool = False, port: int = 7805) -> dict[str, Any]:
    """Run start_marimo with a mocked Popen; capture argv and the token file as Marimo sees it."""
    from dango.notebooks.manager import start_marimo

    seen: dict[str, Any] = {}

    def _popen(cmd: list[str], **_kw: Any) -> MagicMock:
        seen["cmd"] = list(cmd)
        if "--token-password-file" in cmd:
            path = Path(cmd[cmd.index("--token-password-file") + 1])
            seen["token_path"] = path
            seen["token"] = path.read_text()
            seen["mode"] = stat.S_IMODE(path.stat().st_mode)
        proc = MagicMock()
        proc.pid = 12345
        proc.poll.return_value = None
        return proc

    (root / ".dango").mkdir(parents=True, exist_ok=True)
    with (
        patch(f"{_M}.subprocess.Popen", side_effect=_popen),
        patch(f"{_M}._get_idle_timeout", return_value=7200),
        patch(f"{_M}._is_marimo_responding", return_value=True),
        patch(f"{_M}.is_process_running", return_value=False),
        patch(f"{_M}.time.sleep"),
        patch(_CLOUD, return_value=cloud),
        patch(_STATE, return_value="disabled" if cloud else "local"),
    ):
        seen["pid"] = start_marimo(root, port=port)
    return seen


@pytest.mark.unit
def test_local_spawn_uses_token_file_not_no_token(tmp_path: Path) -> None:
    seen = _start(tmp_path)
    cmd = seen["cmd"]
    assert "--no-token" not in cmd
    assert "--token-password-file" in cmd
    assert "--token-password" not in cmd
    assert seen["token_path"] == _token_file(tmp_path)
    assert len(seen["token"]) >= 32
    assert not any(seen["token"] in arg for arg in cmd), "token must never be in argv"


@pytest.mark.unit
def test_token_file_mode_0600_and_dir_private(tmp_path: Path) -> None:
    seen = _start(tmp_path)
    assert seen["mode"] == 0o600


@pytest.mark.unit
def test_token_differs_per_start(tmp_path: Path) -> None:
    first = _start(tmp_path)["token"]
    second = _start(tmp_path)["token"]
    assert first != second
    assert _token_file(tmp_path).read_text() == second


@pytest.mark.unit
def test_token_file_replace_does_not_follow_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    state = tmp_path / ".dango" / "state"
    state.mkdir(parents=True)
    _token_file(tmp_path).symlink_to(victim)
    seen = _start(tmp_path)
    assert victim.read_text() == "keep"
    assert not _token_file(tmp_path).is_symlink()
    assert _token_file(tmp_path).read_text() == seen["token"]


@pytest.mark.unit
def test_cloud_spawn_unchanged(tmp_path: Path) -> None:
    seen = _start(tmp_path, cloud=True)
    assert "--no-token" in seen["cmd"]
    assert "--token-password-file" not in seen["cmd"]
    assert not _token_file(tmp_path).exists()


@pytest.mark.unit
def test_failed_start_removes_token_file(tmp_path: Path) -> None:
    from dango.notebooks.manager import start_marimo

    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = 1  # exited immediately
    proc.returncode = 1
    (tmp_path / ".dango").mkdir()
    with (
        patch(f"{_M}.subprocess.Popen", return_value=proc),
        patch(f"{_M}._get_idle_timeout", return_value=7200),
        patch(f"{_M}.is_process_running", return_value=False),
        patch(f"{_M}.time.sleep"),
        patch(_CLOUD, return_value=False),
        pytest.raises(RuntimeError),
    ):
        start_marimo(tmp_path, port=7805)
    assert not _token_file(tmp_path).exists()


@pytest.mark.unit
def test_stop_removes_token_file(tmp_path: Path) -> None:
    from dango.notebooks.manager import get_marimo_pid_file_path, stop_marimo
    from dango.utils.process import PidRecord

    _start(tmp_path)
    get_marimo_pid_file_path(tmp_path).write_text("{}")
    assert _token_file(tmp_path).exists()
    with (
        patch(f"{_M}.read_pid_record", return_value=PidRecord(pid=12345, start_time=1.0)),
        patch(f"{_M}.is_process_running", return_value=True),
        patch(f"{_M}._stop_marimo_process", return_value="stopped"),
    ):
        assert stop_marimo(tmp_path) is True
    assert not _token_file(tmp_path).exists()


@pytest.mark.unit
def test_open_url_carries_token(tmp_path: Path) -> None:
    from dango.notebooks.access_token import marimo_open_url

    seen = _start(tmp_path)
    url = marimo_open_url(tmp_path, 7805, "nb.py")
    assert url == f"http://localhost:7805/?file=nb.py&access_token={seen['token']}"
    assert marimo_open_url(tmp_path, 7805).endswith(f"/?access_token={seen['token']}")


@pytest.mark.unit
def test_open_url_without_token_file_has_no_token(tmp_path: Path) -> None:
    from dango.notebooks.access_token import marimo_open_url

    assert marimo_open_url(tmp_path, 7805, "nb.py") == "http://localhost:7805/?file=nb.py"


@pytest.mark.unit
def test_probe_uses_public_health_endpoint() -> None:
    """The readiness probe must work with auth on: /health is public, / redirects to login."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from dango.notebooks.manager import _is_marimo_responding

    paths: list[str] = []

    class _H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            paths.append(self.path)
            if self.path == "/health":
                body, code = b'{"status":"healthy"}', 200
            else:
                body, code = b"unauthorized", 401
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert _is_marimo_responding(int(server.server_address[1])) is True
    finally:
        server.shutdown()
        server.server_close()
    assert paths == ["/health"]


@pytest.mark.unit
def test_web_status_url_carries_token(tmp_path: Path) -> None:
    from tests.unit.test_web_notebooks import _client

    seen = _start(tmp_path)
    p = "dango.web.routes.notebooks"
    with (
        patch(f"{p}.get_project_root", return_value=tmp_path),
        patch(f"{p}.get_marimo_status", return_value={"running": True, "port": 7805, "pid": 1}),
        patch(_CLOUD, return_value=False),
    ):
        resp = _client(tmp_path).get("/api/notebooks/status?file=nb")
    assert resp.json()["marimo_url"] == (
        f"http://localhost:7805/?file=nb&access_token={seen['token']}"
    )


@pytest.mark.unit
def test_cli_open_url_carries_token(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from dango.cli.commands.notebook import notebook

    seen = _start(tmp_path)
    (tmp_path / "notebooks").mkdir(exist_ok=True)
    (tmp_path / "notebooks" / "t.py").write_text("x = 1\n")
    opened: list[str] = []

    def _open(url: str) -> bool:
        opened.append(url)
        raise KeyboardInterrupt

    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch("dango.notebooks.locking.acquire_lock", return_value=True),
        patch("dango.notebooks.locking.release_lock"),
        patch("dango.notebooks.locking.refresh_lock"),
        patch(
            "dango.notebooks.manager.get_marimo_status",
            return_value={"running": True, "port": 7805, "pid": 1},
        ),
        patch("dango.notebooks.manager.ensure_marimo_protected", return_value=False),
        patch("webbrowser.open", side_effect=_open),
    ):
        CliRunner().invoke(notebook, ["open", "t"])
    assert opened == [f"http://localhost:7805/?file=t.py&access_token={seen['token']}"]
    assert os.path.exists(_token_file(tmp_path))


# ---------------------------------------------------------------------------
# Pre-fix (tokenless) Marimo is restarted with a token; stale token files are removed
# ---------------------------------------------------------------------------


def _pid_record(root: Path, start_time: float | None = 1.0) -> None:
    import json

    (root / ".dango").mkdir(parents=True, exist_ok=True)
    (root / ".dango" / "marimo.pid").write_text(json.dumps({"pid": 777, "start_time": start_time}))


def _ensure(root: Path, *, running: bool, stop_outcome: str = "stopped", cloud: bool = False):
    from dango.notebooks.manager import ensure_marimo_protected

    with (
        patch(f"{_M}.is_process_running", return_value=running),
        patch(f"{_M}._stop_marimo_process", return_value=stop_outcome) as stopper,
        patch(_CLOUD, return_value=cloud),
    ):
        result = ensure_marimo_protected(root)
    return result, stopper


@pytest.mark.unit
def test_prefix_tokenless_marimo_is_stopped_for_restart(tmp_path: Path) -> None:
    _pid_record(tmp_path)
    restarted, stopper = _ensure(tmp_path, running=True)
    assert restarted is True
    stopper.assert_called_once()
    assert not (tmp_path / ".dango" / "marimo.pid").exists()


@pytest.mark.unit
def test_running_marimo_with_token_is_left_alone(tmp_path: Path) -> None:
    from dango.notebooks.access_token import create_token_file

    _pid_record(tmp_path)
    create_token_file(tmp_path)
    restarted, stopper = _ensure(tmp_path, running=True)
    assert restarted is False
    stopper.assert_not_called()
    assert _token_file(tmp_path).exists()


@pytest.mark.unit
def test_unknown_identity_marimo_is_never_touched(tmp_path: Path) -> None:
    """A pid record without a start time cannot prove Dango started the process."""
    _pid_record(tmp_path, start_time=None)
    restarted, stopper = _ensure(tmp_path, running=True)
    assert restarted is False
    stopper.assert_not_called()


@pytest.mark.unit
def test_cloud_marimo_is_never_restarted_for_a_token(tmp_path: Path) -> None:
    _pid_record(tmp_path)
    restarted, stopper = _ensure(tmp_path, running=True, cloud=True)
    assert restarted is False
    stopper.assert_not_called()


@pytest.mark.unit
def test_no_marimo_recorded_is_a_noop(tmp_path: Path) -> None:
    restarted, stopper = _ensure(tmp_path, running=False)
    assert restarted is False
    stopper.assert_not_called()


@pytest.mark.unit
def test_stale_token_removed_when_recorded_process_is_gone(tmp_path: Path) -> None:
    from dango.notebooks.access_token import create_token_file

    _pid_record(tmp_path)
    create_token_file(tmp_path)
    restarted, stopper = _ensure(tmp_path, running=False)
    assert restarted is False
    stopper.assert_not_called()
    assert not _token_file(tmp_path).exists()


@pytest.mark.unit
def test_token_kept_while_marimo_is_still_starting(tmp_path: Path) -> None:
    """Token exists but no pid record yet (start_marimo writes it after readiness): keep it."""
    from dango.notebooks.access_token import create_token_file

    create_token_file(tmp_path)
    restarted, _stopper = _ensure(tmp_path, running=False)
    assert restarted is False
    assert _token_file(tmp_path).exists()


@pytest.mark.unit
def test_cli_open_and_web_lock_call_the_protection_helper(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from dango.cli.commands.notebook import notebook

    (tmp_path / "notebooks").mkdir(exist_ok=True)
    (tmp_path / "notebooks" / "t.py").write_text("x = 1\n")
    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch("dango.notebooks.locking.acquire_lock", return_value=True),
        patch("dango.notebooks.locking.release_lock"),
        patch("dango.notebooks.locking.refresh_lock"),
        patch("dango.notebooks.manager.ensure_marimo_protected", return_value=False) as ensure,
        patch(
            "dango.notebooks.manager.get_marimo_status",
            return_value={"running": True, "port": 7805, "pid": 1},
        ),
        patch("webbrowser.open", side_effect=KeyboardInterrupt),
    ):
        CliRunner().invoke(notebook, ["open", "t"])
    ensure.assert_called_once_with(tmp_path)

    from tests.unit.test_web_notebooks import _client, _init_db, _seed_notebook

    p = "dango.web.routes.notebooks"
    _init_db(tmp_path)
    _seed_notebook(tmp_path, "nb")
    with (
        patch(f"{p}.get_project_root", return_value=tmp_path),
        patch(f"{p}.ensure_marimo_protected", return_value=False) as ensure_web,
        patch(f"{p}.start_idle_checker"),
        patch(f"{p}.get_marimo_status", return_value={"running": True, "port": 7805, "pid": 1}),
    ):
        resp = _client(tmp_path).post("/api/notebooks/nb/lock", json={})
    assert resp.status_code == 200
    ensure_web.assert_called_once_with(tmp_path)
