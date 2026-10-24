"""tests/unit/test_script_launch_isolation.py

Scripts started from the web UI and from the scheduler run through the runner
launcher when the root-owned marker exists, and exactly as before when it does not.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dango.auth.audit import AuditEvent
from dango.auth.models import Role, User
from dango.platform import run_as

_API = "dango.web.routes.scripts_api"
_HELPERS = "dango.web.routes.scripts_helpers"
_JOBS = "dango.platform.scheduling.jobs"
_SECRET = "declared-secret-value"
_OTHER = "undeclared-secret-value"
_OS_ONLY = "os-environ-secret-value"


def _admin() -> User:
    return User(id="admin-id", email="admin@test.com", role=Role.ADMIN, is_active=True)


def _project(tmp_path: Path, scripts_yml: str | None = None) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "hello.py").write_text("print('hi')\n")
    (tmp_path / ".env").write_text(f"SLACK_URL={_SECRET}\nOTHER_TOKEN={_OTHER}\n")
    (tmp_path / ".dango").mkdir()
    if scripts_yml is None:
        scripts_yml = "scripts:\n  - path: hello.py\n    env: [SLACK_URL, NOT_SET_ONE]\n"
    (tmp_path / ".dango" / "scripts.yml").write_text(scripts_yml)
    return tmp_path


def _fake_popen(returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 4242
    proc.returncode = returncode
    proc.poll.return_value = None
    proc.communicate.return_value = ("out", "")
    return proc


def _fake_sudo(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-sudo"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_as, "uses_isolation", lambda: True)
    monkeypatch.setenv("OS_ONLY_SECRET", _OS_ONLY)
    monkeypatch.setenv("OTHER_TOKEN", _OTHER)


@pytest.fixture
def local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_as, "uses_isolation", lambda: False)


@pytest.fixture
def sudo_standin(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """``sudo kill`` replaced by a plain group signal (same uid in tests)."""
    calls: list[tuple[str, str]] = []

    def fake_kill(role: str, signame: str, target: str) -> bool:
        calls.append((signame, target))
        try:
            os.killpg(abs(int(target)), getattr(signal, f"SIG{signame}"))
        except ProcessLookupError:
            return False
        return True

    monkeypatch.setattr(run_as, "_sudo_kill", fake_kill)
    return calls


async def _run_and_wait(
    project: Path, name: str = "hello.py", timeout: int = 10
) -> tuple[dict[str, Any], dict[str, Any], SimpleNamespace]:
    import dango.web.routes.scripts_helpers as helpers
    from dango.web.routes.scripts_api import run_script

    helpers._running_processes.clear()
    with (
        patch(f"{_API}.get_project_root", return_value=project),
        patch(f"{_API}._get_script_timeout", return_value=timeout),
        patch(f"{_API}.append_log_entry") as log,
        patch(f"{_API}.ws_manager") as ws,
        patch(f"{_HELPERS}.log_auth_event") as audit,
    ):
        ws.broadcast = AsyncMock()
        response = await run_script(name=name, request=SimpleNamespace(client=None), user=_admin())
        body = json.loads(bytes(response.body))
        meta_path = helpers._get_log_dir(project) / body["run_id"] / "meta.json"
        deadline = time.monotonic() + 30
        meta = json.loads(meta_path.read_text())
        while meta.get("status") == "running" and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            meta = json.loads(meta_path.read_text())
    return body, meta, SimpleNamespace(audit=audit, log=log)


class TestWebRunIsolated:
    def test_argv_and_env_are_runner_launch(self, tmp_path: Path, isolated: None) -> None:
        project = _project(tmp_path)
        proc = _fake_popen()
        with patch("subprocess.Popen", return_value=proc) as popen:
            asyncio.run(_run_and_wait(project))
        argv = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        assert argv[:5] == [run_as.SUDO, "-n", "-u", "dango-run", "-H"]
        assert argv[-2:] == ["--", "scripts/hello.py"]
        assert "dango.runtime.isolated_run" in argv
        assert not any(_SECRET in part for part in argv)
        assert kwargs["start_new_session"] is True
        assert kwargs["cwd"] == str(project)

    def test_env_is_base_plus_declared_only(self, tmp_path: Path, isolated: None) -> None:
        project = _project(tmp_path)
        with patch("subprocess.Popen", return_value=_fake_popen()) as popen:
            asyncio.run(_run_and_wait(project))
        env = popen.call_args.kwargs["env"]
        assert env["SLACK_URL"] == _SECRET
        assert "NOT_SET_ONE" not in env
        assert "OTHER_TOKEN" not in env
        assert "OS_ONLY_SECRET" not in env
        assert _OTHER not in env.values()
        assert _OS_ONLY not in env.values()
        assert env["DANGO_PROJECT_ROOT"] == str(project)
        assert env["DANGO_SCRIPT_RUN_ID"]
        assert env["PATH"] == "/srv/dango/venv/bin:/usr/bin:/bin"

    def test_declared_but_unset_is_logged(self, tmp_path: Path, isolated: None) -> None:
        project = _project(tmp_path)
        with patch("subprocess.Popen", return_value=_fake_popen()):
            _body, _meta, seen = asyncio.run(_run_and_wait(project))
        messages = [c.args[0]["message"] for c in seen.log.call_args_list]
        assert any("declared but not set: NOT_SET_ONE" in m for m in messages)

    def test_warehouse_write_runs_unisolated_and_is_marked(
        self, tmp_path: Path, isolated: None
    ) -> None:
        project = _project(
            tmp_path,
            "scripts:\n  - path: hello.py\n    env: [SLACK_URL]\n    warehouse: write\n",
        )
        with patch("subprocess.Popen", return_value=_fake_popen()) as popen:
            body, meta, seen = asyncio.run(_run_and_wait(project))
        argv = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        assert argv == [sys.executable, str(project / "scripts" / "hello.py")]
        assert env["SLACK_URL"] == _SECRET
        assert "OS_ONLY_SECRET" not in env
        assert "OTHER_TOKEN" not in env
        assert body["unisolated"] is True
        assert meta["unisolated"] is True
        event_call = next(
            c for c in seen.audit.call_args_list if c.args[0] == AuditEvent.SCRIPT_RUN
        )
        assert event_call.kwargs["details"]["unisolated"] is True

    def test_preflight_failure_fails_closed(
        self, tmp_path: Path, isolated: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _project(tmp_path)
        monkeypatch.setattr(
            run_as,
            "SUDO",
            _fake_sudo(
                tmp_path, "echo 'runner isolation is broken: /x/.env is readable' >&2\nexit 70"
            ),
        )
        real_popen = subprocess.Popen
        with patch("subprocess.Popen", side_effect=real_popen) as popen:
            _body, meta, _audit = asyncio.run(_run_and_wait(project))
        assert popen.call_count == 1
        assert meta["status"] == "failed"
        assert meta["exit_code"] == 70
        import dango.web.routes.scripts_helpers as helpers

        run_dir = next((helpers._get_log_dir(project)).iterdir())
        assert "runner isolation is broken" in (run_dir / "stderr.txt").read_text()
        assert "unisolated" not in meta


class TestStopThroughRunner:
    def test_timeout_uses_runner_stop_without_blocking_loop(
        self, tmp_path: Path, isolated: None, monkeypatch: pytest.MonkeyPatch, sudo_standin: Any
    ) -> None:
        project = _project(tmp_path)
        monkeypatch.setattr(run_as, "SUDO", _fake_sudo(tmp_path, "exec sleep 30"))
        gaps: list[float] = []

        async def scenario() -> tuple[dict[str, Any], float]:
            async def ticker() -> None:
                last = time.monotonic()
                while True:
                    await asyncio.sleep(0.02)
                    now = time.monotonic()
                    gaps.append(now - last)
                    last = now

            task = asyncio.create_task(ticker())
            started = time.monotonic()
            _body, meta, _audit = await _run_and_wait(project, timeout=1)
            task.cancel()
            return meta, time.monotonic() - started

        meta, elapsed = asyncio.run(scenario())
        assert meta["status"] == "timeout"
        assert elapsed < 8
        assert max(gaps) < 0.5
        assert [s for s, _t in sudo_standin][0] == "TERM"
        import dango.web.routes.scripts_helpers as helpers

        assert "hello.py" not in helpers._running_processes

    def test_cancel_uses_runner_stop_and_clears_handle(
        self, tmp_path: Path, isolated: None, monkeypatch: pytest.MonkeyPatch, sudo_standin: Any
    ) -> None:
        import dango.web.routes.scripts_helpers as helpers
        from dango.web.routes.scripts_api import cancel_script, run_script

        project = _project(tmp_path)
        monkeypatch.setattr(run_as, "SUDO", _fake_sudo(tmp_path, "exec sleep 30"))

        async def scenario() -> float:
            helpers._running_processes.clear()
            with (
                patch(f"{_API}.get_project_root", return_value=project),
                patch(f"{_API}.append_log_entry"),
                patch(f"{_API}.ws_manager") as ws,
                patch(f"{_HELPERS}.log_auth_event"),
            ):
                ws.broadcast = AsyncMock()
                req = SimpleNamespace(client=None)
                await run_script(name="hello.py", request=req, user=_admin())
                started = time.monotonic()
                resp = await cancel_script(name="hello.py", request=req, user=_admin())
                assert json.loads(bytes(resp.body))["status"] == "cancelling"
                took_to_return = time.monotonic() - started
                for _ in range(200):
                    if "hello.py" not in helpers._running_processes:
                        break
                    await asyncio.sleep(0.05)
            return took_to_return

        returned_in = asyncio.run(scenario())
        assert returned_in < 0.5
        assert "hello.py" not in helpers._running_processes
        assert sudo_standin and sudo_standin[0][0] == "TERM"

    def test_zombie_leader_does_not_make_stop_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sudo_standin: Any
    ) -> None:
        from dango.platform import script_launch

        monkeypatch.setattr(run_as, "uses_isolation", lambda: True)
        monkeypatch.setattr(run_as, "SUDO", _fake_sudo(tmp_path, "exec sleep 30"))
        project = _project(tmp_path)
        started = script_launch.start_script(project, "hello.py", project / "scripts" / "hello.py")
        t0 = time.monotonic()
        _out, _err, timed_out = script_launch.communicate_with_timeout(started.proc, 1)
        assert timed_out is True
        assert time.monotonic() - t0 < 4


class TestScheduledRun:
    def _run(self, project: Path, **extra: Any) -> tuple[MagicMock, MagicMock]:
        from dango.platform.scheduling.jobs import run_scheduled_script

        proc = _fake_popen()
        with (
            patch("subprocess.Popen", return_value=proc) as popen,
            patch("dango.utils.activity_log.log_activity"),
            patch(f"{_JOBS}._broadcast"),
            patch(f"{_JOBS}._notify"),
            patch(f"{_JOBS}._try_record_start", return_value=1),
            patch(f"{_JOBS}._try_finish_record"),
            patch(f"{_JOBS}._log_execution_event"),
            patch("dango.platform.notifications.webhook.WebhookSender"),
            patch("dango.platform.notifications.webhook.load_notification_config"),
            patch(f"{_JOBS}._scheduler_service", None),
        ):
            run_scheduled_script(
                "nightly", script_path="hello.py", project_root=str(project), **extra
            )
        return popen, proc

    def test_isolated_reads_scripts_yml_env_and_keeps_schedule_timeout(
        self, tmp_path: Path, isolated: None
    ) -> None:
        project = _project(
            tmp_path, "scripts:\n  - path: hello.py\n    timeout_seconds: 7\n    env: [SLACK_URL]\n"
        )
        popen, proc = self._run(project, timeout_minutes=2)
        argv = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        assert argv[:5] == [run_as.SUDO, "-n", "-u", "dango-run", "-H"]
        assert argv[-1] == "scripts/hello.py"
        assert env["SLACK_URL"] == _SECRET
        assert "OS_ONLY_SECRET" not in env and "OTHER_TOKEN" not in env
        proc.communicate.assert_called_once_with(timeout=120)


class TestLocalModeUnchanged:
    """Marker absent: argv, env, cwd, timeouts exactly as before the runner existed."""

    def test_web_run_golden(self, tmp_path: Path, local_mode: None) -> None:
        project = _project(tmp_path)
        with patch("subprocess.Popen", return_value=_fake_popen()) as popen:
            body, meta, seen = asyncio.run(_run_and_wait(project))
        script = project / "scripts" / "hello.py"
        assert popen.call_args.args[0] == [sys.executable, str(script)]
        kwargs = popen.call_args.kwargs
        assert set(kwargs) == {"cwd", "env", "stdout", "stderr", "text"}
        assert kwargs["cwd"] == str(project)
        assert kwargs["text"] is True
        expected = os.environ.copy()
        expected["DANGO_PROJECT_ROOT"] = str(project)
        expected["DANGO_SCRIPT_RUN_ID"] = body["run_id"]
        assert kwargs["env"] == expected
        assert set(body) == {"status", "run_id", "script_name"}
        assert "unisolated" not in meta
        details = next(
            c for c in seen.audit.call_args_list if c.args[0] == AuditEvent.SCRIPT_RUN
        ).kwargs["details"]
        assert details == {"script_name": "hello.py", "run_id": body["run_id"]}
        assert all("declared" not in c.args[0]["message"] for c in seen.log.call_args_list)

    def test_scheduled_run_golden(self, tmp_path: Path, local_mode: None) -> None:
        project = _project(tmp_path)
        popen, proc = TestScheduledRun()._run(project, timeout_minutes=2)
        script = (project / "scripts" / "hello.py").resolve()
        assert popen.call_args.args[0] == [sys.executable, str(script)]
        kwargs = popen.call_args.kwargs
        assert set(kwargs) == {"cwd", "env", "stdout", "stderr", "text"}
        assert kwargs["cwd"] == str(project)
        expected = os.environ.copy()
        expected["DANGO_PROJECT_ROOT"] = str(project)
        assert kwargs["env"] == expected
        proc.communicate.assert_called_once_with(timeout=120)

    def test_scheduled_timeout_signals_golden(self, tmp_path: Path, local_mode: None) -> None:
        project = _project(tmp_path)
        proc = _fake_popen()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired("x", 60),
            subprocess.TimeoutExpired("x", 5),
            ("", ""),
        ]
        from dango.platform.scheduling.jobs import run_scheduled_script

        with (
            patch("subprocess.Popen", return_value=proc),
            patch("dango.utils.activity_log.log_activity"),
            patch(f"{_JOBS}._broadcast"),
            patch(f"{_JOBS}._notify"),
            patch(f"{_JOBS}._try_record_start", return_value=1),
            patch(f"{_JOBS}._try_finish_record"),
            patch(f"{_JOBS}._log_execution_event"),
            patch("dango.platform.notifications.webhook.WebhookSender"),
            patch("dango.platform.notifications.webhook.load_notification_config"),
            patch(f"{_JOBS}._scheduler_service", None),
        ):
            run_scheduled_script(
                "nightly", script_path="hello.py", project_root=str(project), timeout_minutes=1
            )
        assert proc.communicate.call_args_list[0].kwargs == {"timeout": 60}
        assert proc.communicate.call_args_list[1].kwargs == {"timeout": 5}
        proc.terminate.assert_called_once_with()
        proc.kill.assert_called_once_with()
