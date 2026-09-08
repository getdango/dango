"""tests/unit/test_scripts_wait_for_completion.py

Regression tests for the stdout/stderr race in `_wait_for_completion()`
(1.0.8-Q5). Split out of `test_scripts_api.py` (file-size-check hard
limit is 500 lines) since these tests each launch a real subprocess and
need real wall-clock waits — they don't fit the mock-heavy style of the
rest of the scripts API test suite.

`_wait_for_completion` is a private nested closure inside `run_script()`,
so it can't be imported directly — these tests call the real
`run_script()` route function (bypassing FastAPI's `Depends` resolution
by passing `user`/`request` positionally) with a REAL subprocess, then
poll the on-disk run metadata for completion. A mocked `proc.communicate`
cannot reproduce this bug: it is a genuine race between two independent
`run_in_executor` threads reading the same OS pipes.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dango.auth.models import Role, User

_PATCH_API = "dango.web.routes.scripts_api"


def _make_admin_user() -> User:
    return User(id="admin-id", email="admin@test.com", role=Role.ADMIN, is_active=True)


def _write_script(scripts_dir: Path, name: str, content: str) -> Path:
    """Create a script file and return its path."""
    scripts_dir.mkdir(parents=True, exist_ok=True)
    file_path = scripts_dir / name
    file_path.write_text(content)
    return file_path


async def _run_script_and_wait(
    tmp_path: Path,
    script_content: str,
    timeout_seconds: int,
    script_name: str = "race.py",
    max_wait: float = 20.0,
) -> tuple[dict[str, Any], str, str]:
    """Run a real script through `run_script()` and wait for completion.

    Returns (meta, stdout_text, stderr_text) once the background
    `_wait_for_completion()` task has finished writing results to disk.
    """
    import dango.web.routes.scripts_helpers as helpers
    from dango.web.routes.scripts_api import run_script

    _write_script(tmp_path / "scripts", script_name, script_content)
    helpers._running_processes.clear()

    admin = _make_admin_user()
    fake_request = SimpleNamespace(client=None)

    with (
        patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path),
        patch(f"{_PATCH_API}._get_script_timeout", return_value=timeout_seconds),
        patch(f"{_PATCH_API}.append_log_entry"),
        patch(f"{_PATCH_API}.ws_manager") as mock_ws,
    ):
        mock_ws.broadcast = AsyncMock()

        response = await run_script(name=script_name, request=fake_request, user=admin)
        body = json.loads(bytes(response.body))
        run_id = body["run_id"]

        log_dir = helpers._get_log_dir(tmp_path) / run_id
        meta_path = log_dir / "meta.json"

        deadline = time.monotonic() + max_wait
        meta: dict[str, Any] = json.loads(meta_path.read_text())
        while meta.get("status") == "running":
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"_wait_for_completion() did not finish within {max_wait}s (last meta: {meta})"
                )
            await asyncio.sleep(0.05)
            meta = json.loads(meta_path.read_text())

        stdout_text = (log_dir / "stdout.txt").read_text(encoding="utf-8")
        stderr_text = (log_dir / "stderr.txt").read_text(encoding="utf-8")

    return meta, stdout_text, stderr_text


@pytest.mark.unit
class TestWaitForCompletionRace:
    """Regression tests for the communicate() pipe race (1.0.8-Q5)."""

    def test_wait_for_completion_captures_output_on_normal_exit(self, tmp_path: Path):
        """A fast script that exits before the timeout captures its stdout."""
        meta, stdout_text, _stderr_text = asyncio.run(
            _run_script_and_wait(
                tmp_path,
                script_content="print('hello')\n",
                timeout_seconds=5,
                script_name="normal.py",
            )
        )
        assert meta["status"] == "success"
        assert meta["exit_code"] == 0
        assert "hello" in stdout_text

    def test_wait_for_completion_captures_output_before_terminate(self, tmp_path: Path):
        """Core regression test: output printed before a timeout-triggered
        terminate() must still be captured — this is the exact race fixed by
        reusing a single `communicate()` future via `asyncio.shield()`
        instead of issuing a new competing `run_in_executor` call per stage.
        """
        script = "import sys, time\nprint('before kill', flush=True)\ntime.sleep(10)\n"
        meta, stdout_text, _stderr_text = asyncio.run(
            _run_script_and_wait(
                tmp_path,
                script_content=script,
                timeout_seconds=1,
                script_name="before_kill.py",
            )
        )
        assert meta["status"] == "timeout"
        assert "before kill" in stdout_text

    def test_wait_for_completion_terminate_succeeds_within_grace_period(self, tmp_path: Path):
        """A process that dies promptly on SIGTERM never needs SIGKILL, and
        its pre-terminate output is still captured.
        """
        import dango.web.routes.scripts_helpers as helpers
        from dango.web.routes.scripts_api import run_script

        script = "import sys, time\nprint('before term', flush=True)\ntime.sleep(10)\n"
        script_name = "grace.py"
        _write_script(tmp_path / "scripts", script_name, script)
        helpers._running_processes.clear()

        admin = _make_admin_user()
        fake_request = SimpleNamespace(client=None)

        async def _run() -> tuple[dict[str, Any], str, MagicMock]:
            with (
                patch(f"{_PATCH_API}.get_project_root", return_value=tmp_path),
                patch(f"{_PATCH_API}._get_script_timeout", return_value=1),
                patch(f"{_PATCH_API}.append_log_entry"),
                patch(f"{_PATCH_API}.ws_manager") as mock_ws,
            ):
                mock_ws.broadcast = AsyncMock()

                response = await run_script(name=script_name, request=fake_request, user=admin)
                body = json.loads(bytes(response.body))
                run_id = body["run_id"]

                # The real Popen object is stored synchronously before
                # run_script() returns — spy on its real .kill so we can
                # assert it was never invoked, while the process itself
                # (and its SIGTERM handling) stays completely real.
                proc = helpers._running_processes[script_name]
                kill_spy = MagicMock(wraps=proc.kill)
                proc.kill = kill_spy  # type: ignore[method-assign]

                log_dir = helpers._get_log_dir(tmp_path) / run_id
                meta_path = log_dir / "meta.json"

                deadline = time.monotonic() + 20.0
                meta = json.loads(meta_path.read_text())
                while meta.get("status") == "running":
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"did not finish in time (last meta: {meta})")
                    await asyncio.sleep(0.05)
                    meta = json.loads(meta_path.read_text())

                stdout_text = (log_dir / "stdout.txt").read_text(encoding="utf-8")
            return meta, stdout_text, kill_spy

        meta, stdout_text, kill_spy = asyncio.run(_run())

        assert meta["status"] == "timeout"
        assert "before term" in stdout_text
        kill_spy.assert_not_called()

    def test_wait_for_completion_requires_kill(self, tmp_path: Path):
        """A process that ignores SIGTERM forces the SIGKILL escalation path,
        and output produced before the kill is still captured correctly.
        """
        script = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('before kill', flush=True)\n"
            "time.sleep(30)\n"
        )
        meta, stdout_text, _stderr_text = asyncio.run(
            _run_script_and_wait(
                tmp_path,
                script_content=script,
                timeout_seconds=1,
                script_name="requires_kill.py",
                max_wait=30.0,
            )
        )
        assert meta["status"] == "timeout"
        assert "before kill" in stdout_text
        # SIGKILL exit: returncode is the negative signal number on POSIX.
        assert meta["exit_code"] < 0
